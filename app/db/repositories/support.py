# app/db/repositories/support.py
import logging
from typing import List, Dict, Any, Optional, Tuple
from app.db.connection import Database

logger = logging.getLogger(__name__)


class SupportRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create_ticket(self, user_id: int, vk_user_id: int, text: str,
                            attachment_url: Optional[str] = None) -> int:
        query = """
            INSERT INTO support_tickets (user_id, vk_user_id, text, attachment_url, status)
            VALUES (?, ?, ?, ?, 'open')
        """
        cursor = await self.db.connection.execute(query, (user_id, vk_user_id, text, attachment_url))
        await self.db.connection.commit()
        return cursor.lastrowid

    async def get_tickets_paginated(self, page: int = 1, limit: int = 5) -> Tuple[List[Dict[str, Any]], int]:
        """Возвращает список тикетов (от новых к старым) и общее количество."""
        offset = (page - 1) * limit

        # Получаем общее количество
        count_cursor = await self.db.connection.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'")
        count_row = await count_cursor.fetchone()
        total_count = count_row[0] if count_row else 0

        # Получаем данные (ORDER BY id DESC гарантирует порядок от новых к старым)
        query = """
            SELECT id, vk_user_id, text, attachment_url, status, created_at 
            FROM support_tickets
            WHERE status = 'open'
            ORDER BY id DESC 
            LIMIT ? OFFSET ?
        """
        cursor = await self.db.connection.execute(query, (limit, offset))
        rows = await cursor.fetchall()

        return [dict(row) for row in rows], total_count

    async def get_ticket_by_id(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        """Возвращает полный тикет по ID для детального просмотра."""
        query = """
            SELECT id, user_id, vk_user_id, text, attachment_url, status, created_at 
            FROM support_tickets 
            WHERE id = ?
        """
        cursor = await self.db.connection.execute(query, (ticket_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def resolve_ticket(self, ticket_id: int) -> bool:
        """Меняет статус тикета на 'resolved'. Возвращает True, если обновление прошло успешно."""
        query = "UPDATE support_tickets SET status = 'resolved' WHERE id = ? AND status = 'open'"
        cursor = await self.db.connection.execute(query, (ticket_id,))
        await self.db.connection.commit()
        return cursor.rowcount > 0