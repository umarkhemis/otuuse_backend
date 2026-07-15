import os

# ── 1. config.py - add Cloudinary settings ───────────────────────────────────
config_path = "app/core/config.py"
with open(config_path) as f:
    config = f.read()

old_storage = '    STORAGE_MAX_UPLOAD_MB: int = 8'
new_storage = (
    '    STORAGE_MAX_UPLOAD_MB: int = 8\n'
    '\n'
    '    # Cloudinary (used when STORAGE_PROVIDER=cloudinary)\n'
    '    CLOUDINARY_CLOUD_NAME: str = ""\n'
    '    CLOUDINARY_API_KEY: str = ""\n'
    '    CLOUDINARY_API_SECRET: str = ""'
)

if old_storage in config and 'CLOUDINARY_CLOUD_NAME' not in config:
    config = config.replace(old_storage, new_storage, 1)
    with open(config_path, 'w') as f:
        f.write(config)
    print("config.py: Cloudinary settings added")
else:
    print("config.py: already has Cloudinary or anchor not found")

# ── 2. storage.py - add Cloudinary provider ───────────────────────────────────
storage_path = "app/services/storage.py"
with open(storage_path) as f:
    storage = f.read()

# Update provider to accept cloudinary
old_init = '    def __init__(self):\n        self.provider = settings.STORAGE_PROVIDER'
new_init = (
    '    def __init__(self):\n'
    '        self.provider = settings.STORAGE_PROVIDER\n'
    '\n'
    '    def _build_photo_key(self, folder: str, filename: str) -> str:\n'
    '        ext = os.path.splitext(filename)[1].lower() or ".jpg"\n'
    '        import uuid as _uuid\n'
    '        return f"{folder}/{_uuid.uuid4().hex}{ext}"\n'
    '\n'
    '    async def upload_photo(\n'
    '        self,\n'
    '        content: bytes,\n'
    '        filename: str,\n'
    '        folder: str = "delivery-photos",\n'
    '    ) -> str:\n'
    '        """\n'
    '        Upload a photo and return a public-accessible URL.\n'
    '        Cloudinary -> returns secure_url directly.\n'
    '        Local -> returns a key served at /api/v1/files/{key}.\n'
    '        """\n'
    '        key = self._build_photo_key(folder, filename)\n'
    '        if self.provider == "cloudinary":\n'
    '            return await self._upload_cloudinary(content, folder)\n'
    '        elif self.provider == "local":\n'
    '            await self._upload_local(key, content)\n'
    '            return key\n'
    '        elif self.provider == "s3":\n'
    '            await self._upload_s3(key, content)\n'
    '            return await self.get_url(key)\n'
    '        raise StorageError(f"Unknown storage provider: {self.provider}")'
)

if old_init in storage:
    storage = storage.replace(old_init, new_init, 1)
else:
    print("WARNING: storage.py __init__ anchor not found")

# Add Cloudinary upload method before the singleton line
cloudinary_method = '''
    # ── Cloudinary ────────────────────────────────────────────────────────────
    async def _upload_cloudinary(self, content: bytes, folder: str) -> str:
        """Upload to Cloudinary and return the secure public URL."""
        import asyncio
        try:
            import cloudinary
            import cloudinary.uploader
        except ImportError:
            raise StorageError(
                "cloudinary package not installed. Run: pip install cloudinary"
            )
        cloudinary.config(
            cloud_name=settings.CLOUDINARY_CLOUD_NAME,
            api_key=settings.CLOUDINARY_API_KEY,
            api_secret=settings.CLOUDINARY_API_SECRET,
            secure=True,
        )
        def _do_upload():
            result = cloudinary.uploader.upload(
                content,
                folder=folder,
                resource_type="image",
            )
            return result["secure_url"]
        url = await asyncio.get_event_loop().run_in_executor(None, _do_upload)
        logger.info("cloudinary_upload_success", folder=folder, url=url[:60])
        return url

'''

singleton_anchor = '# ── Singleton ──'
if singleton_anchor in storage and '_upload_cloudinary' not in storage:
    storage = storage.replace(singleton_anchor, cloudinary_method + singleton_anchor, 1)
else:
    print("WARNING: Cloudinary method already present or anchor not found")

with open(storage_path, 'w') as f:
    f.write(storage)
print("storage.py: Cloudinary provider + upload_photo method added")

# ── 3. models.py - add photo URL columns to Delivery ─────────────────────────
models_path = "app/models/models.py"
with open(models_path) as f:
    models = f.read()

old_admin_notes = '    admin_notes = Column(Text)'
new_admin_notes = (
    '    admin_notes = Column(Text)\n'
    '    passenger_photo_url = Column(Text, nullable=True)   # photo of item from passenger\n'
    '    admin_photo_url = Column(Text, nullable=True)        # photo from admin (e.g. proof of receipt)'
)

if old_admin_notes in models and 'passenger_photo_url' not in models:
    models = models.replace(old_admin_notes, new_admin_notes, 1)
    with open(models_path, 'w') as f:
        f.write(models)
    print("models.py: photo URL columns added to Delivery")
else:
    print("models.py: already has photo columns or anchor not found")
