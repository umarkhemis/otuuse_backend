path = "app/api/routes/routes.py"
with open(path) as f:
    content = f.read()

new_endpoint = '''

@router.get("/delivery-status/{delivery_id}")
async def get_delivery_status(
    delivery_id: str,
    current_user: Annotated[User, Depends(get_current_passenger)],
    db: AsyncSession = Depends(get_db),
):
    """
    Passenger polls this every 15 seconds after creating a delivery request.
    Returns the delivery status and the latest admin reply (if any).
    Flutter shows new replies as agent messages in the chat.
    """
    from app.models.models import Delivery, Message, MessageRole
    from sqlalchemy import select as _sel, desc as _desc
    from uuid import UUID as _UUID

    delivery = await db.get(Delivery, _UUID(delivery_id))
    if not delivery or delivery.passenger_id != current_user.id:
        raise HTTPException(status_code=404, detail="Delivery not found")

    # Get the latest agent relay message for this delivery
    # (saved by the admin reply endpoint after LLM voices the admin message)
    result = await db.execute(
        _sel(Message)
        .where(
            Message.delivery_id == delivery.id,
            Message.role.in_([MessageRole.AGENT, MessageRole.ADMIN]),
        )
        .order_by(_desc(Message.created_at))
        .limit(1)
    )
    latest = result.scalar_one_or_none()

    return {
        "status": delivery.status.value,
        "admin_reply": latest.content if latest else None,
        "replied_at": latest.created_at.isoformat() if latest else None,
    }
'''

# Insert before the driver section separator
anchor = '\n# ────────────────────────────────────────────────────────────────────────────────\n"""\napp/api/routes/driver.py - Driver operations\n"""'
if anchor in content:
    content = content.replace(anchor, new_endpoint + anchor, 1)
    with open(path, "w") as f:
        f.write(content)
    print("Done - GET /chat/delivery-status/{delivery_id} added to routes.py")
else:
    print("ERROR: driver section anchor not found in routes.py")
