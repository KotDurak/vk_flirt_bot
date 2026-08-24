from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from yookassa import Configuration, Payment
from yookassa.domain.common.confirmation_type import ConfirmationType
from yookassa.domain.models.receipt import Receipt, ReceiptItem

from app.config import get_settings
from app.services.payments.base import PaymentProvider, PaymentResult, PaymentStatus

logger = logging.getLogger(__name__)


class YooKassaPaymentProvider(PaymentProvider):
    """
    Провайдер платежей через ЮKassa.
    """

    def __init__(self):
        settings = get_settings()
        # Инициализация SDK
        Configuration.account_id = settings.yookassa_shop_id
        Configuration.secret_key = settings.yookassa_secret_key
        self.return_url = settings.yookassa_return_url

    async def create_invoice(
            self,
            user_id: int,
            amount: int,  # Внимание: убедись, что amount передается в копейках (например, 10000 = 100.00 ₽)
            messages: int,
    ) -> PaymentResult:
        invoice_id = f"bot_{user_id}_{uuid.uuid4().hex[:8]}"

        # Форматируем сумму в рубли (ЮKassa ожидает строку вида "100.00")
        # Если amount уже в рублях, убери деление на 100
        amount_str = f"{float(amount):.2f}"

        try:
            # Создаем платеж в ЮKassa
            payment = Payment.create({
                "amount": {
                    "value": amount_str,
                    "currency": "RUB"
                },
                "capture": True,
                "confirmation": {
                    "type": ConfirmationType.REDIRECT,
                    'return_url': self.return_url,
                },
                "description": f"Пополнение баланса: {messages} сообщений",
                # Метаданные, чтобы потом понять, чей это платеж (опционально, но полезно)
                "metadata": {
                    "user_id": str(user_id),
                    "messages": str(messages)
                }
            }, invoice_id)  # Используем invoice_id как ключ идемпотентности

            logger.info(
                "💳 YOOKASSA: Created payment %s for user %s (%s₽, %d msgs). Status: %s",
                payment.id, user_id, amount_str, messages, payment.status
            )

            return PaymentResult(
                success=True,
                invoice_id=payment.id,  # Сохраняем реальный ID платежа из ЮKassa
                payment_url=payment.confirmation.confirmation_url,
            )

        except Exception as e:
            logger.error("❌ YOOKASSA: Failed to create payment for user %s: %s", user_id, e)
            return PaymentResult(
                success=False,
                invoice_id="",
                payment_url="",
                error_message="Не удалось создать платеж. Попробуйте позже."
            )

    async def check_status(self, invoice_id: str) -> PaymentStatus:
        """
        Проверяет статус платежа в ЮKassa по его ID.
        """
        try:
            payment = Payment.find_one(invoice_id)

            if not payment:
                logger.warning("⚠️ YOOKASSA: Payment %s not found", invoice_id)
                return PaymentStatus(is_paid=False, status="not_found")

            # Маппинг статусов ЮKassa на наши статусы
            # Возможные статусы: pending, waiting_for_capture, succeeded, canceled
            is_paid = payment.status == "succeeded"

            logger.info(
                "🔍 YOOKASSA: Checked status for %s. Status: %s, Paid: %s",
                invoice_id, payment.status, is_paid
            )

            return PaymentStatus(
                is_paid=is_paid,
                status=payment.status,
            )

        except Exception as e:
            logger.error("❌ YOOKASSA: Failed to check status for %s: %s", invoice_id, e)
            return PaymentStatus(is_paid=False, status="error")