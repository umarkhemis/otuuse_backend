"""
Sets passwords for existing users that don't have one yet.
Run AFTER the migration:
    python3 set_passwords.py
"""
import asyncio
import psycopg2
from dotenv import dotenv_values

config = dotenv_values('/home/uk/otuuse/otuuse_backend/.env')
url = config['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')

import sys
sys.path.insert(0, '/home/uk/otuuse/otuuse_backend')
from app.core.security import hash_password

conn = psycopg2.connect(url)
conn.autocommit = True
cur = conn.cursor()

# Set admin password
ADMIN_PASSWORD = "Admin@2026"   # change after first login
cur.execute("""
    UPDATE users
    SET password_hash = %s,
        must_change_password = false
    WHERE phone_number = '+256700000002'
    RETURNING name, phone_number
""", (hash_password(ADMIN_PASSWORD),))
row = cur.fetchone()
if row:
    print(f"Admin password set: {row[0]} ({row[1]}) → '{ADMIN_PASSWORD}'")

# Reset test driver to use pin as password (must change on next login)
DRIVER_INITIAL = "1234"   # matches the initial PIN set during onboarding
cur.execute("""
    UPDATE users
    SET password_hash = %s,
        must_change_password = true
    WHERE phone_number = '+256700000001'
    RETURNING name, phone_number
""", (hash_password(DRIVER_INITIAL),))
row = cur.fetchone()
if row:
    print(f"Test driver password set: {row[0]} ({row[1]}) → '{DRIVER_INITIAL}' (must change)")

# Show all users without passwords
cur.execute("""
    SELECT name, phone_number, role
    FROM users
    WHERE password_hash IS NULL
    ORDER BY role, name
""")
rows = cur.fetchall()
if rows:
    print(f"\nUsers still without passwords ({len(rows)}):")
    for r in rows:
        print(f"  {r[2]:12} {r[0]:20} {r[1]}")
else:
    print("\nAll users now have passwords.")

conn.close()
