path = "app/api/routes/routes.py"
with open(path) as f:
    content = f.read()

warnings = []

# 1. Add photo upload imports at top if not present
old_import = "from fastapi import APIRouter, Depends, HTTPException"
new_import = "from fastapi import APIRouter, Depends, File, HTTPException, UploadFile"
if old_import in content and 'UploadFile' not in content:
    content = content.replace(old_import, new_import, 1)
else:
    warnings.append("import anchor not found or UploadFile already present")

# 2. Add passenger photo upload endpoint before the driver section
photo_endpoint = '''

@router.post("/delivery/{delivery_id}/photo")
async def upload_delivery_photo(
    delivery_id: str,
    current_user: Annotated[User, Depends(get_current_passenger)],
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
):
    """
    Passenger uploads a photo of the item to be delivered.
    Stored via the configured storage provider (Cloudinary / local / S3).
    Returned URL is stored on the delivery and sent to the admin.
    """
    from app.models.models import Delivery
    from sqlalchemy import update as _update
    from uuid import UUID as _UUID
    from app.services.storage import storage_service, StorageError

    delivery = await db.get(Delivery, _UUID(delivery_id))
    if not delivery or delivery.passenger_id != current_user.id:
        raise HTTPException(status_code=404, detail="Delivery not found")

    allowed = {".jpg", ".jpeg", ".png", ".webp"}
    ext = ("." + file.filename.rsplit(".", 1)[-1].lower()) if file.filename and "." in file.filename else ""
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="Only JPG, PNG, or WebP images are accepted")

    from app.core.config import settings as _s
    content_bytes = await file.read()
    if len(content_bytes) > _s.STORAGE_MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File exceeds {_s.STORAGE_MAX_UPLOAD_MB}MB limit")

    try:
        url = await storage_service.upload_photo(
            content=content_bytes,
            filename=file.filename or f"photo{ext}",
            folder="delivery-passenger-photos",
        )
    except StorageError as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    await db.execute(
        _update(Delivery).where(Delivery.id == delivery.id).values(passenger_photo_url=url)
    )
    await db.commit()

    # Notify admins that passenger added a photo
    from app.services.notifications import notification_service
    await notification_service.notify_admin_new_delivery(delivery_id=delivery.id, db=db)

    return {"url": url, "message": "Photo uploaded successfully"}
'''

# Insert before driver section
anchor = '\n# ────────────────────────────────────────────────────────────────────────────────\n"""\napp/api/routes/driver.py - Driver operations\n"""'
if anchor in content:
    content = content.replace(anchor, photo_endpoint + anchor, 1)
else:
    warnings.append("driver section anchor not found - photo endpoint not added")

# 3. Update delivery-status endpoint to return photo URLs
old_return = '''    return {
        "status": delivery.status.value,
        "admin_reply": latest.content if latest else None,
        "replied_at": latest.created_at.isoformat() if latest else None,
    }'''

new_return = '''    return {
        "status": delivery.status.value,
        "admin_reply": latest.content if latest else None,
        "replied_at": latest.created_at.isoformat() if latest else None,
        "passenger_photo_url": delivery.passenger_photo_url,
        "admin_photo_url": delivery.admin_photo_url,
    }'''

if old_return in content:
    content = content.replace(old_return, new_return, 1)
else:
    warnings.append("delivery-status return anchor not found")

with open(path, 'w') as f:
    f.write(content)

if warnings:
    print("WARNINGS:")
    for w in warnings:
        print(f"  - {w}")
else:
    print("Done - routes.py: passenger photo upload + photo URLs in delivery-status")
