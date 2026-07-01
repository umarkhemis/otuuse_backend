"""
app/services/sms.py
--------------------
SMS delivery via Africa's Talking.
Used for OTP delivery during authentication.
Africa's Talking has strong Uganda coverage and is widely used.
"""

import africastalking

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_initialized = False


def _init_at():
    global _initialized
    if not _initialized:
        africastalking.initialize(
            username=settings.AFRICASTALKING_USERNAME,
            api_key=settings.AFRICASTALKING_API_KEY,
        )
        _initialized = True


class SMSService:

    async def send(self, phone: str, message: str) -> bool:
        """
        Send an SMS to a single phone number.
        Phone must be in E.164 format e.g. +256701234567.
        Returns True if sent successfully.
        """
        try:
            _init_at()
            sms = africastalking.SMS

            # Africa's Talking is synchronous - run in executor to not block
            import asyncio
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: sms.send(
                    message=message,
                    recipients=[phone],
                    sender_id=settings.AFRICASTALKING_SENDER_ID,
                )
            )

            # Check response
            recipients = response.get("SMSMessageData", {}).get("Recipients", [])
            if recipients and recipients[0].get("statusCode") == 101:
                logger.info("sms_sent", phone=phone)
                return True
            else:
                logger.warning("sms_not_delivered", phone=phone, response=response)
                return False

        except Exception as e:
            logger.error("sms_send_error", phone=phone, error=str(e))
            return False


# ── Singleton ──────────────────────────────────────────────────────────────────
sms_service = SMSService()
