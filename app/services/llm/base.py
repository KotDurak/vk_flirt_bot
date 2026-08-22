from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    """Результат генерации от любого LLM-провайдера."""
    success: bool
    content: str = ""
    error_code: int | None = None
    error_message: str = ""


class LLMBase(ABC):
    """
    Абстрактный базовый класс для всех LLM-провайдеров.

    Все провайдеры (Polza, Yandex, OpenRouter и т.д.) должны
    наследоваться от этого класса и реализовывать метод generate().
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    @abstractmethod
    async def generate(
            self,
            messages: list[dict[str, str]],
            max_retries: int = 2,
            model_override: str | None = None
    ) -> LLMResult:
        """
        Генерирует ответ на основе списка сообщений.

        Args:
            messages: Список сообщений в формате OpenAI
                     [{"role": "system", "content": "..."}, ...]
            max_retries: Максимальное количество попыток при ошибках
            model_override: Модель для генерации

        Returns:
            LLMResult с результатом генерации
        """
        pass

    def _count_tokens_approx(self, messages: list[dict]) -> int:
        """Грубая оценка количества токенов (1 токен ≈ 4 символа)."""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        return total_chars // 4