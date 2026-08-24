from __future__ import annotations

import logging

from app.config import get_settings
from app.services.payments.base import PaymentProvider
from app.services.payments.test_provider import TestPaymentProvider
from app.services.payments.yookassa_provider import YooKassaPaymentProvider

logger = logging.getLogger(__name__)


def create_payment_provider() -> PaymentProvider:
    """
    Создаёт провайдер платежей на основе конфигурации.

    Читает PAYMENT_PROVIDER из .env и возвращает соответствующий провайдер.
    """
    settings = get_settings()
    provider = getattr(settings, 'payment_provider', 'test').lower()
    print(provider)
    if provider == "test":
        logger.info("💳 Using TestPaymentProvider")
        return TestPaymentProvider()

    elif provider == "yookassa":
        return YooKassaPaymentProvider()

    # Здесь потом добавим реальные провайдеры:
    # elif provider == "platega":
    #     from app.services.payments.platega import PlategaProvider
    #     logger.info("💳 Using PlategaProvider")
    #     return PlategaProvider()

    else:
        raise ValueError(f"Unknown payment provider: {provider}")