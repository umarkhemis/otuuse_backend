import asyncio
import getpass
from sqlalchemy import select, update
from app.db.session import AsyncSessionLocal
from app.models.models import User, UserRole
from app.core.security import hash_password


async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(User).where(User.role == UserRole.ADMIN)
        )).scalars().all()

        print("Admin accounts in the database:")
        for u in rows:
            print(f"  {u.name:20} {u.phone_number}")

        phone = input("\nPhone to reset (e.g. +2567XXXXXXXX): ").strip()
        new_pw = getpass.getpass("New password (hidden): ")
        if len(new_pw) < 6:
            print("Password must be at least 6 characters.")
            return

        result = await db.execute(
            update(User)
            .where(User.phone_number == phone, User.role == UserRole.ADMIN)
            .values(password_hash=hash_password(new_pw), must_change_password=False)
        )
        await db.commit()
        print("Password reset." if result.rowcount else "No admin found with that phone number.")


asyncio.run(main())
