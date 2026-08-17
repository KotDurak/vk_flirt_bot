# app/db/repositories/users.py
from __future__ import annotations
import logging
import aiosqlite
from app.db.connection import Database

logger = logging.getLogger(__name__)


class UserRepository:
    """Отвечает за работу с таблицей users."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # app/db/repositories/users.py

    async def get_or_create(self, vk_user_id: int) -> dict:
        """Получает или создаёт пользователя."""
        conn = self.db.connection

        cursor = await conn.execute(
            "SELECT id, vk_user_id,created_at,is_premium,messages FROM users WHERE vk_user_id = ?", (vk_user_id,)
        )
        row = await cursor.fetchone()

        if row:
            return {
                "id": row[0],
                "vk_user_id": row[1],
                "created_at": row[2],
                "is_premium": row[3],
                "messages": row[4],
            }

        # Создаём нового пользователя
        await conn.execute(
            "INSERT INTO users (vk_user_id, messages) VALUES (?, 80)",
            (vk_user_id,),
        )
        await conn.commit()

        cursor = await conn.execute(
            "SELECT * FROM users WHERE vk_user_id = ?", (vk_user_id,)
        )
        row = await cursor.fetchone()

        return {
            "id": row[0],
            "vk_user_id": row[1],
            "created_at": row[2],
            "is_premium": row[3],
            "messages": row[4],
        }