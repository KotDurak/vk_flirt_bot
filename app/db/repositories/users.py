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

    async def get_or_create(self, vk_user_id: int) -> dict:
        """
        Ищет пользователя по VK ID.
        Если не находит - создает нового и возвращает его.
        """
        conn = self.db.connection

        # 1. Пытаемся найти
        cursor = await conn.execute(
            "SELECT id, vk_user_id, is_premium FROM users WHERE vk_user_id = ?",
            (vk_user_id,)
        )
        row = await cursor.fetchone()

        if row:
            # dict(row) превращает aiosqlite.Row в обычный словарь
            return dict(row)

        # 2. Если не нашли - создаем
        logger.info("Creating new user for vk_id=%s", vk_user_id)
        cursor = await conn.execute(
            "INSERT INTO users (vk_user_id) VALUES (?)",
            (vk_user_id,)
        )
        await conn.commit()

        return {
            "id": cursor.lastrowid,
            "vk_user_id": vk_user_id,
            "is_premium": False
        }