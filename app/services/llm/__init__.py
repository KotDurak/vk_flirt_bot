"""
Модуль LLM-клиентов.

Предоставляет абстрактный интерфейс для работы с различными
LLM-провайдерами (Polza, Yandex, OpenRouter и т.д.).

Использование:
    from app.services.llm import create_llm_client

    llm = create_llm_client(session)
    result = await llm.generate(messages)
"""

from app.services.llm.base import LLMBase, LLMResult
from app.services.llm.factory import create_llm_client

__all__ = [
    "LLMBase",
    "LLMResult",
    "create_llm_client",
]