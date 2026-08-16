# app/db/repositories/messages.py
from __future__ import annotations
import logging
from typing import Optional
from app.db.connection import Database

logger = logging.getLogger(__name__)


class MessageRepository:
    """Отвечает за работу с историей сообщений."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add_message(
            self,
            user_id: int,
            character_id: int,
            role: str,
            content: str
    ) -> int:
        """Сохраняет сообщение и возвращает его ID."""
        conn = self.db.connection
        cursor = await conn.execute(
            "INSERT INTO messages (user_id, character_id, role, content) VALUES (?, ?, ?, ?)",
            (user_id, character_id, role, content)
        )
        await conn.commit()
        return cursor.lastrowid

    async def get_recent_history(
            self,
            user_id: int,
            character_id: int,
            limit: int = 20,
            after_message_id: int = 0
    ) -> list[dict]:
        """Возвращает последние N сообщений после указанного ID."""
        conn = self.db.connection
        cursor = await conn.execute(
            """
            SELECT role, content, id FROM messages
            WHERE user_id = ? AND character_id = ? AND id > ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, character_id, after_message_id, limit)
        )
        rows = await cursor.fetchall()
        result = [{"role": row["role"], "content": row["content"], "id": row["id"]} for row in reversed(rows)]
        logger.info("📋 History loaded: %d messages (after id=%d)", len(result), after_message_id)
        return result

    async def count_messages(self, user_id: int, character_id: int) -> int:
        """Возвращает количество сообщений для пары пользователь-персонаж."""
        conn = self.db.connection
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id = ? AND character_id = ?",
            (user_id, character_id)
        )
        return (await cursor.fetchone())[0]

    async def get_messages_for_summary(
            self,
            user_id: int,
            character_id: int,
            from_message_id: int,
            keep_last: int = 10
    ) -> list[dict]:
        """
        Возвращает сообщения для генерации summary:
        от from_message_id до (последнее - keep_last).
        """
        conn = self.db.connection
        # Сначала находим ID сообщения, до которого нужно брать
        cursor = await conn.execute(
            """
            SELECT id FROM messages
            WHERE user_id = ? AND character_id = ?
            ORDER BY id DESC LIMIT 1 OFFSET ?
            """,
            (user_id, character_id, keep_last)
        )
        boundary_row = await cursor.fetchone()
        if not boundary_row:
            return []

        boundary_id = boundary_row["id"]

        # Берем сообщения от from_message_id до boundary_id
        cursor = await conn.execute(
            """
            SELECT id, role, content FROM messages
            WHERE user_id = ? AND character_id = ? AND id > ? AND id <= ?
            ORDER BY id ASC
            """,
            (user_id, character_id, from_message_id, boundary_id)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_last_message_id(self, user_id: int, character_id: int) -> Optional[int]:
        """Возвращает ID последнего сообщения."""
        conn = self.db.connection
        cursor = await conn.execute(
            """
            SELECT id FROM messages
            WHERE user_id = ? AND character_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, character_id)
        )
        row = await cursor.fetchone()
        return row["id"] if row else None

    async def clear_history(
            self,
            user_id: int,
            character_id: Optional[int] = None
    ) -> None:
        """Очищает историю сообщений (для /reset)."""
        conn = self.db.connection
        if character_id:
            await conn.execute(
                "DELETE FROM messages WHERE user_id = ? AND character_id = ?",
                (user_id, character_id)
            )
        else:
            await conn.execute(
                "DELETE FROM messages WHERE user_id = ?",
                (user_id,)
            )
        await conn.commit()

    async def delete_old_messages(
            self,
            user_id: int,
            character_id: int,
            before_message_id: int
    ) -> None:
        """Удаляет сообщения старше указанного ID (после суммаризации)."""
        conn = self.db.connection
        await conn.execute(
            """
            DELETE FROM messages 
            WHERE user_id = ? AND character_id = ? AND id < ?
            """,
            (user_id, character_id, before_message_id)
        )
        await conn.commit()