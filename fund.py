import asyncio
from sqlalchemy import text
from app.db.session import AsyncSessionLocal

PHONE = "+256782705521"   # test driver's number

async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("UPDATE users SET wallet_balance_ugx = 20000 WHERE phone_number = :p"),
            {"p": PHONE},
        )
        await db.commit()
        print("funded")

asyncio.run(main())
