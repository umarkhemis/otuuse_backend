path = "app/tasks/dispatch_tasks.py"
with open(path) as f:
    content = f.read()

new_task = '''

@celery_app.task(name="app.tasks.dispatch_tasks.remind_admins_delivery", bind=True, max_retries=3)
def remind_admins_delivery(self, delivery_id: str):
    """
    Re-notifies all admins about a pending delivery request that hasn't
    been replied to yet. Called 5 minutes after delivery creation, and
    retries every 5 minutes up to 3 times (15 minutes total).
    """
    async def _run():
        from app.db.session import get_standalone_db_session
        from app.models.models import Delivery, DeliveryStatus, MessageRole, Message
        from sqlalchemy import select, desc

        async with get_standalone_db_session() as db:
            delivery = await db.get(Delivery, uuid.UUID(delivery_id))
            if not delivery:
                return  # Delivery no longer exists

            if delivery.status != DeliveryStatus.PENDING:
                return  # Already handled by an admin

            # Check if any admin has replied
            result = await db.execute(
                select(Message)
                .where(
                    Message.delivery_id == delivery.id,
                    Message.role.in_([MessageRole.ADMIN, MessageRole.AGENT]),
                )
                .limit(1)
            )
            if result.scalar_one_or_none():
                return  # Admin already replied

            # Still no reply - resend FCM to all admins
            from app.services.notifications import notification_service
            await notification_service.notify_admin_new_delivery(
                delivery_id=delivery.id, db=db
            )
            logger.info("admin_delivery_reminder_sent", delivery_id=delivery_id)

            # Schedule another reminder in 5 minutes
            raise self.retry(countdown=300)

    _run_async(_run())
'''

# Append before the last line of the file
if "remind_admins_delivery" not in content:
    content = content.rstrip() + "\n" + new_task
    with open(path, "w") as f:
        f.write(content)
    print("Done - remind_admins_delivery Celery task added")
else:
    print("Task already exists - skipped")
