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

        # ДОБАВЛЕНЫ: referred_by, referral_bonus_claimed
        cursor = await conn.execute(
            "SELECT id, vk_user_id, created_at, is_premium, messages, preferred_model, referred_by, referral_bonus_claimed FROM users WHERE vk_user_id = ?",
            (vk_user_id,)
        )
        row = await cursor.fetchone()

        if row:
            return {
                "id": row[0],
                "vk_user_id": row[1],
                "created_at": row[2],
                "is_premium": row[3],
                "messages": row[4],
                "preferred_model": row[5],
                "referred_by": row[6],
                "referral_bonus_claimed": row[7]
            }

        # Создаём нового пользователя
        await conn.execute(
            "INSERT INTO users (vk_user_id, messages) VALUES (?, 80)",
            (vk_user_id,),
        )
        await conn.commit()

        cursor = await conn.execute(
            "SELECT id, vk_user_id, created_at, is_premium, messages, preferred_model, referred_by, referral_bonus_claimed FROM users WHERE vk_user_id = ?",
            (vk_user_id,)
        )
        row = await cursor.fetchone()

        return {
            "id": row[0],
            "vk_user_id": row[1],
            "created_at": row[2],
            "is_premium": row[3],
            "messages": row[4],
            "preferred_model": row[5],
            "referred_by": row[6],  # <-- ДОБАВЛЕНО
            "referral_bonus_claimed": row[7]  # <-- ДОБАВЛЕНО
        }

    async def update_preferred_model(self, vk_user_id: int, model_name: str) -> None:
        """Обновляет предпочтенную модель для пользователя."""
        await self.db.connection.execute(
            "UPDATE users SET preferred_model = ? WHERE vk_user_id = ?",
            (model_name, vk_user_id)
        )
        await self.db.connection.commit()

    async def get_by_vk_id(self, vk_user_id: int) -> dict | None:
        """Получает пользователя по его VK ID, не создавая нового."""
        query = "SELECT * FROM users WHERE vk_user_id = ?"
        cursor = await self.db.connection.execute(query, (vk_user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def refill_all_messages(self, target_amount: int) -> int:
        """
        Массовое пополнение: устанавливает колонку messages = target_amount
        всем, у кого она меньше. Возвращает количество затронутых строк.
        """
        query = """
            UPDATE users 
            SET messages = ? 
            WHERE messages < ?
        """
        cursor = await self.db.connection.execute(query, (target_amount, target_amount))
        await self.db.connection.commit()
        return cursor.rowcount

    async def set_referred_by(self, user_id: int, referrer_id: int) -> None:
        """Привязывает пользователя к тому, кто его пригласил.
        Условие 'referred_by IS NULL' защищает от перепривязки,
        если пользователь уже был кем-то приглашен ранее."""
        await self.db.connection.execute(
            """
            UPDATE users 
            SET referred_by = ? 
            WHERE id = ? AND referred_by IS NULL
            """,
            (referrer_id, user_id)
        )
        await self.db.connection.commit()

    async def mark_referral_bonus_claimed(self, user_id: int) -> None:
        """Ставит флаг, что пользователь уже получил бонус за приглашение."""
        await self.db.connection.execute(
            """
            UPDATE users 
            SET referral_bonus_claimed = TRUE 
            WHERE id = ?
            """,
            (user_id,)
        )
        await self.db.connection.commit()

    async def get_by_vk_id_strict(self, vk_user_id: int) -> dict | None:
        """Получает пользователя строго по VK ID (для поиска реферера)."""
        query = "SELECT id FROM users WHERE vk_user_id = ?"
        cursor = await self.db.connection.execute(query, (vk_user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None