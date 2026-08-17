from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PaymentResult:
    """Результат создания инвойса."""
    success: bool
    invoice_id: str = ""
    payment_url: str = ""
    error_message: str = ""


@dataclass
class PaymentStatus:
    """Статус платежа."""
    is_paid: bool
    status: str  # "pending", "paid", "failed"
    amount: int = 0


class PaymentProvider(ABC):
    """Абстрактный класс для платёжных систем."""

    @abstractmethod
    async def create_invoice(
            self,
            user_id: int,
            amount: int,
            messages: int,
    ) -> PaymentResult:
        """
        Создаёт инвойс на оплату.

        Args:
            user_id: ID пользователя
            amount: Сумма в рублях
            messages: Количество сообщений

        Returns:
            PaymentResult с invoice_id и payment_url
        """
        pass

    @abstractmethod
    async def check_status(self, invoice_id: str) -> PaymentStatus:
        """
        Проверяет статус инвойса.

        Args:
            invoice_id: ID инвойса

        Returns:
            PaymentStatus с информацией о статусе
        """
        pass