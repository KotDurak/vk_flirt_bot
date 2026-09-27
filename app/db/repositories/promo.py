# app/db/repositories/promo.py
from __future__ import annotations
import logging
from app.db.connection import Database

logger = logging.getLogger(__name__)


class PromoRepository:
    """Отвечает за работу с таблицами promo_codes и promo_usage."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_active_promo(self, code: str) -> dict | None:
        """Получает активный промокод. Возвращает None, если код не найден, неактивен или исчерпал лимит."""
        query = """
            SELECT id, code, reward, max_uses, current_uses, is_active 
            FROM promo_codes 
            WHERE code = ? AND is_active = 1
        """
        cursor = await self.db.connection.execute(query, (code.upper(),))
        row = await cursor.fetchone()

        if not row:
            return None

        promo = {
            "id": row[0],
            "code": row[1],
            "reward": row[2],
            "max_uses": row[3],  # Теперь это может быть None
            "current_uses": row[4],
            "is_active": bool(row[5])
        }

        # ✅ ПРОВЕРКА ДЛЯ БАРСИКА: Если лимит задан (не None) И он достигнут
        if promo["max_uses"] is not None and promo["current_uses"] >= promo["max_uses"]:
            return None

        return promo

    async def has_user_used_promo(self, user_id: int, promo_code_id: int) -> bool:
        """Проверяет, использовал ли уже этот пользователь данный промокод."""
        query = """
            SELECT 1 FROM promo_usage 
            WHERE user_id = ? AND promo_code_id = ?
        """
        cursor = await self.db.connection.execute(query, (user_id, promo_code_id))
        row = await cursor.fetchone()
        return row is not None

    async def apply_promo_code(self, user_id: int, promo_code_id: int) -> None:
        """
        Записывает факт использования промокода пользователем и увеличивает счетчик.
        Автоматически деактивирует промокод, если исчерпан лимит (без триггеров БД).
        """
        await self.db.connection.execute("BEGIN")
        try:
            # 1. Записываем, что юзер использовал код
            await self.db.connection.execute(
                "INSERT INTO promo_usage (user_id, promo_code_id) VALUES (?, ?)",
                (user_id, promo_code_id)
            )

            # 2. Увеличиваем общий счетчик использований кода
            await self.db.connection.execute(
                "UPDATE promo_codes SET current_uses = current_uses + 1 WHERE id = ?",
                (promo_code_id,)
            )

            # Здесь промокод может быть деактивирован
            await self.db.connection.execute("""
                UPDATE promo_codes 
                SET is_active = 0 
                WHERE id = ? AND max_uses IS NOT NULL AND current_uses >= max_uses
            """, (promo_code_id,))

            await self.db.connection.commit()
        except Exception as e:
            await self.db.connection.rollback()
            logger.error("Failed to apply promo code: %s", e)
            raise

    async def create_promo_code(self, code: str, reward: int, max_uses: int | None = None) -> None:
        """
        Создает новый промокод.
        max_uses: int — жесткий лимит.
        max_uses: None — безлимитное количество активаций (но всё ещё 1 раз на пользователя).
        """
        await self.db.connection.execute(
            """
            INSERT INTO promo_codes (code, reward, max_uses, current_uses, is_active) 
            VALUES (?, ?, ?, 0, 1)
            """,
            (code.upper(), reward, max_uses)  # SQLite корректно сохранит None как NULL
        )
        await self.db.connection.commit()

    async def get_all_active_promos(self, limit: int = 10, offset: int = 0) -> tuple[list[dict], int]:
        """
        Получает список активных промокодов с пагинацией.
        Возвращает кортеж: (список промокодов, общее количество).
        """
        # 1. Узнаем общее количество для расчета страниц
        count_cursor = await self.db.connection.execute(
            "SELECT COUNT(*) FROM promo_codes WHERE is_active = 1"
        )
        total = (await count_cursor.fetchone())[0]

        # 2. Получаем только нужную порцию данных
        query = """
            SELECT code, reward, max_uses, current_uses 
            FROM promo_codes 
            WHERE is_active = 1 
            ORDER BY id DESC 
            LIMIT ? OFFSET ?
        """
        cursor = await self.db.connection.execute(query, (limit, offset))
        rows = await cursor.fetchall()

        promos = [
            {
                "code": row[0],
                "reward": row[1],
                "max_uses": row[2],
                "current_uses": row[3]
            }
            for row in rows
        ]

        return promos, total