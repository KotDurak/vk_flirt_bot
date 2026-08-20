from __future__ import annotations

import asyncio
import json
import logging
import random

import aiohttp

from app.config import get_llm_settings
from app.services.llm.base import LLMBase, LLMResult

logger = logging.getLogger(__name__)


class LLMVseGPT(LLMBase):
    """
    Клиент для VseGPT.ru API (полностью OpenAI-совместимый).
    Отличается стабильностью, поэтому используем Exponential Backoff
    вместо глобальных блокировок.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._settings = get_llm_settings()

    async def generate(
            self,
            messages: list[dict[str, str]],
            max_retries: int = 3,  # Для стабильных провайдеров можно 3
    ) -> LLMResult:
        logger.info(
            "🚀 VseGPT request: model=%s, msgs=%d, tokens≈%d",
            self._settings.model,
            len(messages),
            self._count_tokens_approx(messages)
        )

        # Убедимся, что URL не имеет двойных слешей
        base_url = self._settings.base_url.rstrip('/')
        url = f"{base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self._settings.model,
            "messages": messages,
            "max_tokens": self._settings.max_tokens,
            "temperature": self._settings.temperature,
            "top_p": self._settings.top_p,
            "frequency_penalty": self._settings.frequency_penalty,
            "presence_penalty": self._settings.presence_penalty,
        }

        for attempt in range(max_retries):
            try:
                async with self._session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120)
                ) as response:
                    body = await response.text()

                    if response.status == 200:
                        data = json.loads(body)
                        choices = data.get("choices", [])

                        if not choices:
                            return LLMResult(success=False, error_message="Empty choices from VseGPT")

                        finish_reason = choices[0].get("finish_reason")
                        content = choices[0].get("message", {}).get("content", "")

                        logger.info(
                            "✅ VseGPT response: finish_reason=%s, chars=%d",
                            finish_reason, len(content)
                        )

                        if finish_reason == "length":
                            logger.warning("⚠️ VseGPT response cut off by max_tokens!")

                        return LLMResult(success=True, content=content.strip())

                    # --- Обработка ошибок ---

                    # 429 Too Many Requests (Rate Limit)
                    if response.status == 429:
                        wait_time = (2 ** attempt) + random.uniform(0, 1)  # Exponential backoff + jitter
                        logger.warning(
                            "⚠️ VseGPT 429 Rate Limit (attempt %d/%d). Backing off for %.1fs...",
                            attempt + 1, max_retries, wait_time
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    # 400 Bad Request (чаще всего context_length_exceeded)
                    if response.status == 400:
                        logger.error("VseGPT 400 error: %s", body[:500])
                        return LLMResult(
                            success=False,
                            error_code=400,
                            error_message="context_too_long_or_invalid_request"
                        )

                    # 5xx Server Errors (временные проблемы на стороне провайдера)
                    if response.status >= 500:
                        wait_time = (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(
                            "VseGPT 5xx Server Error (attempt %d/%d). Retrying in %.1fs...",
                            attempt + 1, max_retries, wait_time
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    # Прочие ошибки (401 Unauthorized, 404 Not Found и т.д.)
                    logger.error("VseGPT unexpected error %s: %s", response.status, body[:500])
                    return LLMResult(
                        success=False,
                        error_code=response.status,
                        error_message=body[:500]
                    )

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                wait_time = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "Network error (attempt %d/%d): %s. Retrying in %.1fs...",
                    attempt + 1, max_retries, exc, wait_time
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)
                    continue

                return LLMResult(success=False, error_code=0, error_message=str(exc))

        return LLMResult(
            success=False,
            error_code=0,
            error_message="Max retries exceeded for VseGPT"
        )