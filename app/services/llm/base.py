from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
from datetime import datetime
from pathlib import Path

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

    def log(self, messages: list, target_model: str, payload: dict, content: str, usage: dict) -> None:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "model": target_model,
            "messages": messages,
            "settings": {
                "temperature": payload.get("temperature"),
                "repetition_penalty": payload.get("repetition_penalty"),
                "frequency_penalty": payload.get("frequency_penalty"),
                "presence_penalty": payload.get("presence_penalty"),
                "min_p": payload.get("min_p"),
            },
            "response": content,
            "tokens_in": usage.get("prompt_tokens") if usage else None,
            "tokens_out": usage.get("completion_tokens") if usage else None,
        }

        log_dir = Path("logs/llm_requests")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}_requests.jsonl"

        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"❌ Failed to write log: {e}")