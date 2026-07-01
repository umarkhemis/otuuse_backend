"""
app/services/agent/context.py
-------------------------------
Manages the conversation window passed to the LLM on every request.
Fetches the last N messages from PostgreSQL and formats them for the LLM API.
"""

from uuid import UUID
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import Message, MessageRole


class ConversationContextService:

    async def get_window(
        self,
        user_id: UUID,
        db: AsyncSession,
    ) -> list[dict]:
        """
        Fetch the last N messages for a user and format them as
        LLM-compatible message dicts (role: user|assistant, content: str).

        The window size is controlled by LLM_CONVERSATION_WINDOW in settings.
        Only user and agent messages are included - admin messages are excluded
        from the passenger's context window.
        """
        window_size = settings.LLM_CONVERSATION_WINDOW

        result = await db.execute(
            select(Message)
            .where(
                Message.user_id == user_id,
                Message.role.in_([MessageRole.USER, MessageRole.AGENT]),
            )
            .order_by(desc(Message.created_at))
            .limit(window_size)
        )

        messages = result.scalars().all()

        # Reverse to chronological order (we fetched newest-first)
        messages = list(reversed(messages))

        # Format for LLM API
        # MessageRole.USER -> "user"
        # MessageRole.AGENT -> "assistant"
        formatted = []
        for msg in messages:
            role = "user" if msg.role == MessageRole.USER else "assistant"
            formatted.append({
                "role": role,
                "content": msg.content,
            })

        return formatted
