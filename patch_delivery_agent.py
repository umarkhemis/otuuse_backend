path = "app/services/agent/agent.py"
with open(path) as f:
    content = f.read()

warnings = []

# 1. Replace the missing-info check (LLM) with a hardcoded delivery-specific question
old_missing = '''            reply = await self._generate_response(
                history=history,
                context_note="User wants to send something but some details are missing. Ask for: what item, from where, to where, and if it is urgent.",
                user_message=user_message,
            user_name=user_name,
            )
            return AgentResponse(message=reply, intent=MessageIntent.DELIVERY_REQUEST)'''

new_missing = '''            missing = []
            if not extracted.delivery_item:
                missing.append("what you'd like delivered")
            if not extracted.delivery_from:
                missing.append("where we should pick it up from")
            if not extracted.delivery_to:
                missing.append("where it needs to go")
            missing_str = " and ".join(missing) if missing else "a few details"
            return AgentResponse(
                message=(
                    f"I'd be happy to arrange that delivery for you! "
                    f"Could you please tell me {missing_str}?"
                ),
                intent=MessageIntent.DELIVERY_REQUEST,
            )'''

if old_missing in content:
    content = content.replace(old_missing, new_missing, 1)
else:
    warnings.append("delivery missing-info anchor not found")

# 2. Replace the success response with hardcoded message + Celery 5-min reminder
old_success = '''        context_note = (
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
        )'''

new_success = '''        # Schedule a 5-minute admin reminder in case no one responds
        try:
            from app.tasks.dispatch_tasks import remind_admins_delivery
            remind_admins_delivery.apply_async(
                args=[str(delivery.id)],
                countdown=300,  # 5 minutes
            )
        except Exception as _e:
            logger.warning("delivery_reminder_schedule_failed", error=str(_e))

        urgency_note = " This has been marked as urgent." if extracted.is_urgent else ""
        return AgentResponse(
            message=(
                f"Your delivery request has been received!{urgency_note} "
                "Our team will review it and get back to you shortly "
                "with availability and pricing. Please hold on."
            ),
            intent=MessageIntent.DELIVERY_REQUEST,
            delivery_id=delivery.id,
        )'''

if old_success in content:
    content = content.replace(old_success, new_success, 1)
else:
    warnings.append("delivery success anchor not found")

with open(path, "w") as f:
    f.write(content)

if warnings:
    print("WARNINGS:")
    for w in warnings:
        print(f"  - {w}")
else:
    print("Done - delivery handler: hardcoded messages + 5-min Celery reminder scheduled")
