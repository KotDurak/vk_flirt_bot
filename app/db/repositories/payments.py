from __future__ import annotations

import logging
from typing import Optional

from app.db.connection import Database

logger = logging.getLogger(__name__)


class PaymentRepository:
    """Репозиторий для работы с платежами и балансом."""

    def __init__(self, db: Database):
        self.db = db

    async def create_payment(
            self,
            user_id: int,
            invoice_id: str,
            amount: int,
            messages: int,
    ) -> None:
        """Создаёт запись о новом платеже."""
        conn = self.db.connection
        await conn.execute(
            """
            INSERT INTO payments (user_id, invoice_id, amount, messages, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (user_id, invoice_id, amount, messages),
        )
        await conn.commit()

    async def get_payment_by_invoice(self, invoice_id: str) -> Optional[dict]:
        """Получает платёж по invoice_id."""
        conn = self.db.connection
        cursor = await conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?",
            (invoice_id,),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "user_id": row[1],
                "invoice_id": row[2],
                "amount": row[3],
                "messages": row[4],
                "status": row[5],
                "created_at": row[6],
                "paid_at": row[7],
            }
        return None

    async def mark_as_paid(self, invoice_id: str) -> None:
        """Отмечает платёж как оплаченный."""
        conn = self.db.connection
        await conn.execute(
            """
            UPDATE payments 
            SET status = 'paid', paid_at = CURRENT_TIMESTAMP
            WHERE invoice_id = ?
            """,
            (invoice_id,),
        )
        await conn.commit()

    async def add_user_messages(self, user_id: int, messages: int) -> None:
        """Начисляет пользователю сообщения (энергию)."""
        conn = self.db.connection
        await conn.execute(
            "UPDATE users SET messages = messages + ? WHERE id = ?",
            (messages, user_id),
        )
        await conn.commit()

    async def get_user_balance(self, user_id: int) -> int:
        """Получает баланс сообщений пользователя."""
        conn = self.db.connection
        cursor = await conn.execute(
            "SELECT messages FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def use_message(self, user_id: int) -> bool:
        """Списывает одно сообщение. Возвращает True, если успешно."""
        return await self.deduct_messages(user_id, 1)

    async def deduct_messages(self, user_id: int, amount: int) -> bool:
        """
        Списывает указанное количество сообщений.
        Возвращает True, если списание прошло успешно (было достаточно средств).
        """
        conn = self.db.connection
        # Важно: условие messages >= ? гарантирует, что баланс не уйдет в минус
        cursor = await conn.execute(
            """
            UPDATE users 
            SET messages = messages - ? 
            WHERE id = ? AND messages >= ?
            """,
            (amount, user_id, amount),
        )
        await conn.commit()

        # Проверяем, действительно ли строка была обновлена
        return cursor.rowcount > 0

    async def get_user_stats(self, user_id: int) -> dict:
        """Получает статистику пользователя."""
        conn = self.db.connection

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        total_messages = row[0] if row else 0

        cursor = await conn.execute(
            "SELECT COALESCE(SUM(messages), 0) FROM payments WHERE user_id = ? AND status = 'paid'",
            (user_id,),
        )
        row = await cursor.fetchone()
        total_energy_bought = row[0] if row else 0

        return {
            "total_messages": total_messages,
            "total_energy_bought": total_energy_bought,
        }