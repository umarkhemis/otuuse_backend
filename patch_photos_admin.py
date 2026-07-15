path = "app/api/routes/admin.py"
with open(path) as f:
    content = f.read()

warnings = []

# Add admin photo upload endpoint - insert after the reply endpoint
# Find a unique anchor at the end of the reply endpoint
admin_photo_endpoint = '''

@router.post("/deliveries/{delivery_id}/photo")
async def admin_upload_delivery_photo(
    delivery_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
):
    """
    Admin uploads a photo related to the delivery
    (e.g. photo of item before pickup, or proof of delivery).
    URL is stored on the delivery and returned to the passenger via polling.
    """
    from app.services.storage import storage_service, StorageError
    from sqlalchemy import update as _update

    delivery = await db.get(Delivery, uuid.UUID(delivery_id))
    if not delivery:
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
            folder="delivery-admin-photos",
        )
    except StorageError as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    await db.execute(
        _update(Delivery).where(Delivery.id == delivery.id).values(admin_photo_url=url)
    )
    await log_admin_action(
        db,
        admin_id=current_admin.id,
        action="upload_delivery_photo",
        target_type="delivery",
        target_id=delivery_id,
    )
    await db.commit()

    return {"url": url, "message": "Photo uploaded"}
'''

# Insert before the audit log section
anchor = '\n# ── Audit Log ──'
if anchor in content and 'admin_upload_delivery_photo' not in content:
    content = content.replace(anchor, admin_photo_endpoint + anchor, 1)
    with open(path, 'w') as f:
        f.write(content)
    print("Done - admin.py: admin delivery photo upload endpoint added")
else:
    print("WARNING: anchor not found or endpoint already present")
