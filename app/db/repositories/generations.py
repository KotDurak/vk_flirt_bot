import logging
from typing import Optional
from datetime import datetime, timedelta
from app.db.connection import Database

logger = logging.getLogger(__name__)

class GenerationRepository:
    """Репозиторий для управления логами и кулдаунами генерации изображений."""

    def __init__(self, db: Database):
        self.db = db

    async def get_last_attempt(self, user_id: int) -> Optional[datetime]:
        """Возвращает время последней попытки генерации пользователя."""
        conn = self.db.connection
        cursor = await conn.execute(
            """
            SELECT attempted_at FROM user_generations 
            WHERE user_id = ? 
            ORDER BY attempted_at DESC 
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if row:
            # Преобразуем строку времени в объект datetime
            time_str = row[0]
            if isinstance(time_str, str):
                return datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            return time_str
        return None

    async def log_attempt(self, user_id: int, status: str, error_reason: str = None) -> None:
        """Записывает факт попытки генерации в базу."""
        conn = self.db.connection
        await conn.execute(
            """
            INSERT INTO user_generations (user_id, status, error_reason)
            VALUES (?, ?, ?)
            """,
            (user_id, status, error_reason),
        )
        await conn.commit()

    async def update_last_attempt_status(self, user_id: int, status: str, error_reason: str = None) -> None:
        """Обновляет статус последней 'pending' попытки пользователя."""
        conn = self.db.connection
        await conn.execute(
            """
            UPDATE user_generations 
            SET status = ?, error_reason = ?, attempted_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND status = 'pending'
            ORDER BY attempted_at DESC
            LIMIT 1
            """,
            (status, error_reason, user_id),
        )
        await conn.commit()

    async def log_attempt(self, user_id: int, status: str, error_reason: str = None) -> None:
        """Записывает факт попытки генерации в базу."""
        # Валидация статуса
        if status not in ('pending', 'success', 'error'):
            raise ValueError(f"Invalid status: {status}. Must be 'pending', 'success', or 'error'")

        conn = self.db.connection
        await conn.execute(
            """
            INSERT INTO user_generations (user_id, status, error_reason)
            VALUES (?, ?, ?)
            """,
            (user_id, status, error_reason),
        )
        await conn.commit()