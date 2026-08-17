# app/services/payments/test_provider.py
from __future__ import annotations

import logging
import uuid

from app.services.payments.base import PaymentProvider, PaymentResult, PaymentStatus

logger = logging.getLogger(__name__)


class TestPaymentProvider(PaymentProvider):
    """
    Тестовый провайдер для отладки.
    Всегда возвращает 'оплачено' при проверке статуса.
    """

    # Задержка в секундах перед "оплатой" (для реалистичности)
    PAYMENT_DELAY = 5  # 5 секунд для тестирования

    async def create_invoice(
            self,
            user_id: int,
            amount: int,
            messages: int,
    ) -> PaymentResult:
        invoice_id = f"test_{uuid.uuid4().hex[:8]}"

        logger.info(
            "🧪 TEST: Created invoice %s for user %s (%d₽, %d msgs)",
            invoice_id, user_id, amount, messages
        )

        return PaymentResult(
            success=True,
            invoice_id=invoice_id,
            payment_url=f"https://example.com/test-payment/{invoice_id}",
        )

    async def check_status(self, invoice_id: str) -> PaymentStatus:
        """
        В тестовом режиме всегда возвращает 'оплачено'.
        Для реалистичности можно добавить задержку.
        """
        logger.info("🧪 TEST: Checking status for invoice %s", invoice_id)

        # Вариант 1: Всегда оплачено (мгновенно)
        return PaymentStatus(
            is_paid=True,
            status="paid",
        )

        # Вариант 2: С задержкой (раскомментируй, если хочешь реалистичнее)
        # Для этого нужно хранить время создания в БД