"""
app/services/audit.py
-----------------------
Lightweight audit logging for admin actions.

Usage: call log_admin_action() inside the same DB transaction as the action
it's recording - it only adds to the session, it does not commit. The
caller's existing db.commit() persists the entry alongside the action it
documents, so the two are always written atomically together.
"""
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AdminAuditLog


async def log_admin_action(
    db: AsyncSession,
    admin_id,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    entry = AdminAuditLog(
        id=uuid.uuid4(),
        admin_id=admin_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )
    db.add(entry)
