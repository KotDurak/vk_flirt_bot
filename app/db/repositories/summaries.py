# app/db/repositories/summaries.py
from __future__ import annotations
import logging
from typing import Optional
from app.db.connection import Database

logger = logging.getLogger(__name__)


class SummaryRepository:
    """Отвечает за работу с summary диалогов."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_summary(self, user_id: int, character_id: int) -> Optional[dict]:
        """Возвращает summary для пары пользователь-персонаж."""
        conn = self.db.connection
        cursor = await conn.execute(
            """
            SELECT summary, last_summarized_message_id 
            FROM conversation_summary 
            WHERE user_id = ? AND character_id = ?
            """,
            (user_id, character_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def save_summary(
            self,
            user_id: int,
            character_id: int,
            summary: str,
            last_summarized_message_id: int
    ) -> None:
        """Сохраняет или обновляет summary."""
        conn = self.db.connection
        await conn.execute(
            """
            INSERT INTO conversation_summary (user_id, character_id, summary, last_summarized_message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, character_id) DO UPDATE SET
                summary = excluded.summary,
                last_summarized_message_id = excluded.last_summarized_message_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, character_id, summary, last_summarized_message_id)
        )
        await conn.commit()

    async def clear_summary(
            self,
            user_id: int,
            character_id: Optional[int] = None
    ) -> None:
        """Очищает summary (для /reset)."""
        conn = self.db.connection
        if character_id:
            await conn.execute(
                "DELETE FROM conversation_summary WHERE user_id = ? AND character_id = ?",
                (user_id, character_id)
            )
        else:
            await conn.execute(
                "DELETE FROM conversation_summary WHERE user_id = ?",
                (user_id,)
            )
        await conn.commit()