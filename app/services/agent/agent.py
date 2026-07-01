"""
app/services/agent/agent.py
----------------------------
The AI Agent - the brain of the entire platform.

Responsibilities:
- Receive passenger messages
- Classify intent (ride, delivery, status, etc.)
- Extract structured data (locations, delivery details)
- Geocode locations via Nominatim
- Calculate fares via ORS
- Coordinate with dispatch service
- Relay admin messages for deliveries
- Maintain conversation context

LLM Provider: Groq (fast, free tier - switch to Anthropic for production)
Model: llama-3.1-8b-instant (Groq) -> claude-haiku (Anthropic in production)

The agent uses a two-pass approach:
  Pass 1 - Intent + data extraction (structured JSON output)
  Pass 2 - Natural language response generation

This separation makes the logic reliable and testable.
"""

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import MessageIntent, MessageRole, RideStatus, DeliveryStatus
from app.services.geocoding import geocoding_service, GeocodingError, Coordinates
from app.services.routing import routing_service, RoutingError
from app.services.agent.llm_client import llm_client
from app.services.agent.system_prompt import build_system_prompt
from app.services.agent.context import ConversationContextService
from app.services.dispatch import dispatch_service
from app.services import crud

logger = get_logger(__name__)


# ── Intent + Extraction Result ─────────────────────────────────────────────────

@dataclass
class ExtractedData:
    intent: MessageIntent
    pickup_name: Optional[str] = None
    dropoff_name: Optional[str] = None
    delivery_item: Optional[str] = None
    delivery_from: Optional[str] = None
    delivery_to: Optional[str] = None
    is_urgent: bool = False
    missing_info: Optional[str] = None   # what the agent still needs to ask for


@dataclass
class AgentResponse:
    message: str                         # text to send back to passenger
    intent: MessageIntent
    ride_id: Optional[UUID] = None
    delivery_id: Optional[UUID] = None
    fare_ugx: Optional[int] = None


# ── Agent ──────────────────────────────────────────────────────────────────────

class AgentService:

    def __init__(self):
        self.context_service = ConversationContextService()

    async def process_message(
        self,
        user_id: UUID,
        user_message: str,
        db: AsyncSession,
    ) -> AgentResponse:
        """
        Main entry point. Receives a raw passenger message and returns
        a fully resolved AgentResponse.

        Flow:
          1. Save incoming message
          2. Load conversation window
          3. Extract intent and structured data via LLM (Pass 1)
          4. Execute the appropriate handler based on intent
          5. Generate the natural language reply via LLM (Pass 2)
          6. Save agent reply and return
        """

        # 1. Save incoming message to DB
        await crud.save_message(
            db=db,
            user_id=user_id,
            role=MessageRole.USER,
            content=user_message,
            intent=MessageIntent.NONE,
        )

        # 2. Load conversation window for context
        history = await self.context_service.get_window(user_id=user_id, db=db)

        # 3. Extract intent and structured data
        extracted = await self._extract_intent_and_data(
            user_message=user_message,
            history=history,
        )

        logger.info(
            "agent_intent_classified",
            user_id=str(user_id),
            intent=extracted.intent.value,
        )

        # 4. Execute handler
        response = await self._handle_intent(
            extracted=extracted,
            user_id=user_id,
            user_message=user_message,
            history=history,
            db=db,
        )

        # 5. Save agent reply
        await crud.save_message(
            db=db,
            user_id=user_id,
            role=MessageRole.AGENT,
            content=response.message,
            intent=extracted.intent,
            ride_id=response.ride_id,
            delivery_id=response.delivery_id,
        )

        return response

    # ── Pass 1: Intent Extraction ──────────────────────────────────────────────

    async def _extract_intent_and_data(
        self,
        user_message: str,
        history: list[dict],
    ) -> ExtractedData:
        """
        Ask the LLM to classify intent and extract structured data.
        Returns a JSON object - reliable and parseable.
        """

        extraction_prompt = """
You are a data extraction engine for a transport platform in Kabale, Uganda.
Analyze the user message and conversation history, then return ONLY a JSON object.

Intents:
- ride_request: user wants a boda boda ride
- delivery_request: user wants to send a package or item
- status_inquiry: user asking about their current ride or delivery
- cancellation: user wants to cancel their ride or delivery
- general_question: user asking about pricing, availability, etc.
- greeting: hello, hi, good morning, etc.
- unclear: cannot determine intent

For ride_request extract: pickup_name, dropoff_name (null if not mentioned)
For delivery_request extract: delivery_item, delivery_from, delivery_to, is_urgent (bool)

If information is missing for ride_request or delivery_request, set missing_info to a
short description of what is still needed (e.g. "destination" or "pickup location").

Return ONLY this JSON structure, nothing else:
{
  "intent": "<intent>",
  "pickup_name": "<string or null>",
  "dropoff_name": "<string or null>",
  "delivery_item": "<string or null>",
  "delivery_from": "<string or null>",
  "delivery_to": "<string or null>",
  "is_urgent": false,
  "missing_info": "<string or null>"
}
"""

        messages = history[-6:] + [{"role": "user", "content": user_message}]

        try:
            raw = await llm_client.complete(
                system_prompt=extraction_prompt,
                messages=messages,
                max_tokens=300,
            )

            # Strip any accidental markdown fences
            clean = re.sub(r"```(?:json)?|```", "", raw).strip()
            data = json.loads(clean)

            return ExtractedData(
                intent=MessageIntent(data.get("intent", "unclear")),
                pickup_name=data.get("pickup_name"),
                dropoff_name=data.get("dropoff_name"),
                delivery_item=data.get("delivery_item"),
                delivery_from=data.get("delivery_from"),
                delivery_to=data.get("delivery_to"),
                is_urgent=data.get("is_urgent", False),
                missing_info=data.get("missing_info"),
            )

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error("agent_extraction_failed", error=str(e), raw=raw if 'raw' in locals() else "")
            return ExtractedData(intent=MessageIntent.UNCLEAR)

    # ── Pass 2: Natural Language Response Generation ───────────────────────────

    async def _generate_response(
        self,
        history: list[dict],
        context_note: str,
        user_message: str,
    ) -> str:
        """
        Generate a warm, natural language reply based on what has happened.
        context_note injects the outcome of the handler (e.g. fare, driver found).
        """
        system_prompt = build_system_prompt(context_note=context_note)
        messages = history[-settings.LLM_CONVERSATION_WINDOW:] + [
            {"role": "user", "content": user_message}
        ]

        return await llm_client.complete(
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=settings.LLM_MAX_TOKENS,
        )

    # ── Intent Handlers ────────────────────────────────────────────────────────

    async def _handle_intent(
        self,
        extracted: ExtractedData,
        user_id: UUID,
        user_message: str,
        history: list[dict],
        db: AsyncSession,
    ) -> AgentResponse:

        handlers = {
            MessageIntent.RIDE_REQUEST: self._handle_ride_request,
            MessageIntent.DELIVERY_REQUEST: self._handle_delivery_request,
            MessageIntent.STATUS_INQUIRY: self._handle_status_inquiry,
            MessageIntent.CANCELLATION: self._handle_cancellation,
            MessageIntent.GENERAL_QUESTION: self._handle_general_question,
            MessageIntent.GREETING: self._handle_greeting,
            MessageIntent.UNCLEAR: self._handle_unclear,
        }

        handler = handlers.get(extracted.intent, self._handle_unclear)
        return await handler(
            extracted=extracted,
            user_id=user_id,
            user_message=user_message,
            history=history,
            db=db,
        )

    async def _handle_ride_request(
        self,
        extracted: ExtractedData,
        user_id: UUID,
        user_message: str,
        history: list[dict],
        db: AsyncSession,
    ) -> AgentResponse:
        """
        Full ride request flow:
        1. Validate we have both pickup and destination
        2. Geocode both locations
        3. Calculate route and fare
        4. Check for active drivers
        5. Respond with fare quote or missing info request
        (Dispatch is triggered AFTER the passenger confirms the fare)
        """

        # Step 1: Check for missing information
        if extracted.missing_info:
            reply = await self._generate_response(
                history=history,
                context_note=f"The user wants a ride but we still need: {extracted.missing_info}. Ask for it politely.",
                user_message=user_message,
            )
            return AgentResponse(message=reply, intent=MessageIntent.RIDE_REQUEST)

        if not extracted.pickup_name or not extracted.dropoff_name:
            reply = await self._generate_response(
                history=history,
                context_note="The user wants a ride but did not provide both pickup and destination. Ask for what is missing.",
                user_message=user_message,
            )
            return AgentResponse(message=reply, intent=MessageIntent.RIDE_REQUEST)

        # Step 2: Geocode both locations
        try:
            pickup_coords = await geocoding_service.geocode(extracted.pickup_name)
            dropoff_coords = await geocoding_service.geocode(extracted.dropoff_name)
        except GeocodingError:
            reply = await self._generate_response(
                history=history,
                context_note="The location service is temporarily unavailable. Apologise and ask the user to try again shortly.",
                user_message=user_message,
            )
            return AgentResponse(message=reply, intent=MessageIntent.RIDE_REQUEST)

        if not pickup_coords:
            reply = await self._generate_response(
                history=history,
                context_note=f"We could not find the pickup location '{extracted.pickup_name}' on the map. Ask the user to describe it differently or use a nearby landmark.",
                user_message=user_message,
            )
            return AgentResponse(message=reply, intent=MessageIntent.RIDE_REQUEST)

        if not dropoff_coords:
            reply = await self._generate_response(
                history=history,
                context_note=f"We could not find the destination '{extracted.dropoff_name}' on the map. Ask the user to describe it differently or use a nearby landmark.",
                user_message=user_message,
            )
            return AgentResponse(message=reply, intent=MessageIntent.RIDE_REQUEST)

        # Step 3: Get route and calculate fare
        try:
            route = await routing_service.get_route(
                from_lat=pickup_coords.latitude,
                from_lon=pickup_coords.longitude,
                to_lat=dropoff_coords.latitude,
                to_lon=dropoff_coords.longitude,
            )
        except RoutingError:
            reply = await self._generate_response(
                history=history,
                context_note="The routing service is temporarily unavailable. Apologise and ask the user to try again shortly.",
                user_message=user_message,
            )
            return AgentResponse(message=reply, intent=MessageIntent.RIDE_REQUEST)

        # Step 4: Check driver availability
        available_count = await dispatch_service.count_available_drivers(
            pickup_lat=pickup_coords.latitude,
            pickup_lon=pickup_coords.longitude,
            db=db,
        )

        if available_count == 0:
            reply = await self._generate_response(
                history=history,
                context_note=f"No drivers are currently available near {extracted.pickup_name}. Apologise and let the user know we are checking. They can try again in a few minutes.",
                user_message=user_message,
            )
            return AgentResponse(message=reply, intent=MessageIntent.RIDE_REQUEST)

        # Step 5: Save pending ride and quote fare
        # Store ride details in Redis temporarily - confirmed when user says yes
        ride_context = {
            "pickup_name": extracted.pickup_name,
            "dropoff_name": extracted.dropoff_name,
            "pickup_lat": pickup_coords.latitude,
            "pickup_lon": pickup_coords.longitude,
            "dropoff_lat": dropoff_coords.latitude,
            "dropoff_lon": dropoff_coords.longitude,
            "distance_km": route.distance_km,
            "duration_minutes": route.duration_minutes,
            "estimated_fare_ugx": route.estimated_fare_ugx,
        }

        from app.services.cache import get_redis
        redis = await get_redis()
        await redis.setex(
            f"pending_ride:{user_id}",
            300,   # 5 minutes to confirm
            json.dumps(ride_context),
        )

        context_note = (
            f"Fare quote ready. Route: {extracted.pickup_name} to {extracted.dropoff_name}. "
            f"Distance: {route.distance_km}km. "
            f"Estimated time: {round(route.duration_minutes)} minutes. "
            f"Fare: {route.estimated_fare_ugx:,} UGX. "
            f"Drivers available: {available_count}. "
            "Quote this fare clearly and ask the user to confirm. "
            "Tell them a driver will be assigned once they confirm."
        )

        reply = await self._generate_response(
            history=history,
            context_note=context_note,
            user_message=user_message,
        )

        return AgentResponse(
            message=reply,
            intent=MessageIntent.RIDE_REQUEST,
            fare_ugx=route.estimated_fare_ugx,
        )

    async def _handle_delivery_request(
        self,
        extracted: ExtractedData,
        user_id: UUID,
        user_message: str,
        history: list[dict],
        db: AsyncSession,
    ) -> AgentResponse:
        """
        Delivery flow:
        1. Collect delivery details if incomplete
        2. Create a delivery ticket in DB
        3. Alert admin via FCM
        4. Tell passenger admin will get back to them
        """

        if extracted.missing_info:
            reply = await self._generate_response(
                history=history,
                context_note=f"User wants a delivery but we still need: {extracted.missing_info}. Ask politely.",
                user_message=user_message,
            )
            return AgentResponse(message=reply, intent=MessageIntent.DELIVERY_REQUEST)

        if not all([extracted.delivery_item, extracted.delivery_from, extracted.delivery_to]):
            reply = await self._generate_response(
                history=history,
                context_note="User wants to send something but some details are missing. Ask for: what item, from where, to where, and if it is urgent.",
                user_message=user_message,
            )
            return AgentResponse(message=reply, intent=MessageIntent.DELIVERY_REQUEST)

        # Create delivery ticket
        delivery = await crud.create_delivery(
            db=db,
            passenger_id=user_id,
            pickup_name=extracted.delivery_from,
            dropoff_name=extracted.delivery_to,
            item_description=extracted.delivery_item,
            is_urgent=extracted.is_urgent,
        )

        # Alert admin via FCM
        from app.services.notifications import notification_service
        await notification_service.notify_admin_new_delivery(delivery_id=delivery.id, db=db)

        context_note = (
            f"Delivery ticket created (ID: {delivery.id}). "
            f"Item: {extracted.delivery_item}. "
            f"From: {extracted.delivery_from} to {extracted.delivery_to}. "
            f"Urgent: {extracted.is_urgent}. "
            "Tell the user we have received their delivery request and our team will review it and get back to them shortly with pricing and availability. "
            "Do NOT mention ticket IDs or internal references to the user."
        )

        reply = await self._generate_response(
            history=history,
            context_note=context_note,
            user_message=user_message,
        )

        return AgentResponse(
            message=reply,
            intent=MessageIntent.DELIVERY_REQUEST,
            delivery_id=delivery.id,
        )

    async def _handle_status_inquiry(
        self,
        extracted: ExtractedData,
        user_id: UUID,
        user_message: str,
        history: list[dict],
        db: AsyncSession,
    ) -> AgentResponse:
        """Check the user's most recent active ride or delivery and report status."""

        active_ride = await crud.get_active_ride_for_passenger(user_id=user_id, db=db)

        if active_ride:
            status_map = {
                RideStatus.MATCHED: "Your driver has been assigned and is on their way.",
                RideStatus.ACCEPTED: "Your driver has accepted the ride and is heading to you.",
                RideStatus.DRIVER_ARRIVING: "Your driver has arrived at the pickup point.",
                RideStatus.IN_PROGRESS: "Your ride is currently in progress.",
            }
            status_text = status_map.get(active_ride.status, f"Status: {active_ride.status.value}")

            context_note = (
                f"Active ride found. Route: {active_ride.pickup_name} to {active_ride.dropoff_name}. "
                f"Current status: {status_text}. "
                f"Fare: {active_ride.estimated_fare_ugx:,} UGX. "
                "Share this status naturally and reassuringly."
            )
        else:
            active_delivery = await crud.get_active_delivery_for_passenger(user_id=user_id, db=db)
            if active_delivery:
                context_note = (
                    f"Active delivery found. Status: {active_delivery.status.value}. "
                    f"Item: {active_delivery.item_description}. "
                    "Tell the user their delivery is being processed and our team will update them soon."
                )
            else:
                context_note = "No active rides or deliveries found for this user. Let them know they have no active bookings."

        reply = await self._generate_response(
            history=history,
            context_note=context_note,
            user_message=user_message,
        )

        return AgentResponse(message=reply, intent=MessageIntent.STATUS_INQUIRY)

    async def _handle_cancellation(
        self,
        extracted: ExtractedData,
        user_id: UUID,
        user_message: str,
        history: list[dict],
        db: AsyncSession,
    ) -> AgentResponse:

        active_ride = await crud.get_active_ride_for_passenger(user_id=user_id, db=db)

        if active_ride and active_ride.status in [RideStatus.REQUESTED, RideStatus.MATCHED]:
            await crud.cancel_ride(
                db=db,
                ride_id=active_ride.id,
                reason="Cancelled by passenger via chat",
            )
            # Notify driver if already matched
            if active_ride.driver_id:
                from app.services.notifications import notification_service
                await notification_service.notify_driver_ride_cancelled(
                    driver_id=active_ride.driver_id,
                    db=db,
                )
            context_note = "Ride has been cancelled successfully. Confirm this to the user and offer to help them request a new one."
        elif active_ride and active_ride.status in [RideStatus.DRIVER_ARRIVING, RideStatus.IN_PROGRESS]:
            context_note = "The ride cannot be cancelled because the driver has already arrived or the ride is in progress. Explain this politely and advise the user to speak with the driver directly."
        else:
            context_note = "No active ride found to cancel. Let the user know they have no active booking to cancel."

        reply = await self._generate_response(
            history=history,
            context_note=context_note,
            user_message=user_message,
        )

        return AgentResponse(message=reply, intent=MessageIntent.CANCELLATION)

    async def _handle_general_question(
        self,
        extracted: ExtractedData,
        user_id: UUID,
        user_message: str,
        history: list[dict],
        db: AsyncSession,
    ) -> AgentResponse:

        context_note = (
            "User has a general question about the platform. "
            "Platform info: We offer boda boda rides and delivery services in Kabale. "
            "Pricing is dynamic based on distance. "
            "Payment is via in-app wallet (MTN MoMo or Airtel Money). "
            "We operate 24/7. "
            "Answer helpfully based on what was asked."
        )

        reply = await self._generate_response(
            history=history,
            context_note=context_note,
            user_message=user_message,
        )

        return AgentResponse(message=reply, intent=MessageIntent.GENERAL_QUESTION)

    async def _handle_greeting(
        self,
        extracted: ExtractedData,
        user_id: UUID,
        user_message: str,
        history: list[dict],
        db: AsyncSession,
    ) -> AgentResponse:

        user = await crud.get_user_by_id(user_id=user_id, db=db)
        first_name = user.name.split()[0] if user else "there"

        context_note = (
            f"User is greeting. Their name is {first_name}. "
            "Greet them warmly by first name and briefly let them know you can help them get a boda ride or arrange a delivery. "
            "Keep it short - one or two sentences."
        )

        reply = await self._generate_response(
            history=history,
            context_note=context_note,
            user_message=user_message,
        )

        return AgentResponse(message=reply, intent=MessageIntent.GREETING)

    async def _handle_unclear(
        self,
        extracted: ExtractedData,
        user_id: UUID,
        user_message: str,
        history: list[dict],
        db: AsyncSession,
    ) -> AgentResponse:

        context_note = (
            "The user's message is unclear. "
            "Politely ask them to clarify whether they need a boda ride or a delivery service. "
            "Keep it brief and friendly."
        )

        reply = await self._generate_response(
            history=history,
            context_note=context_note,
            user_message=user_message,
        )

        return AgentResponse(message=reply, intent=MessageIntent.UNCLEAR)


# ── Singleton ──────────────────────────────────────────────────────────────────
agent_service = AgentService()
