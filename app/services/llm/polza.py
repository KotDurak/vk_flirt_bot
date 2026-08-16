from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import aiohttp

from app.config import get_llm_settings
from app.services.llm.base import LLMBase, LLMResult

logger = logging.getLogger(__name__)


class LLMPolza(LLMBase):
    """
    Клиент для Polza.ai API (OpenAI-совместимый).
    Использует NextBit как провайдер моделей.
    """

    # Глобальные переменные класса для rate limiting
    _global_cooldown_until: float = 0.0
    _consecutive_429_count: int = 0

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._settings = get_llm_settings()

    async def generate(
            self,
            messages: list[dict[str, str]],
            max_retries: int = 2,
    ) -> LLMResult:
        logger.info(
            "🚀 Polza request: model=%s, msgs=%d, tokens≈%d",
            self._settings.model,
            len(messages),
            self._count_tokens_approx(messages)
        )

        url = f"{self._settings.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._settings.model,
            "messages": messages,
            "max_tokens": self._settings.max_tokens,
            "temperature": self._settings.temperature,
            "frequency_penalty": 0.3,
            "presence_penalty": 0.6,
        }

        for attempt in range(max_retries):
            # Проверяем cooldown перед каждой попыткой
            now = time.time()
            if now < LLMPolza._global_cooldown_until:
                wait_time = LLMPolza._global_cooldown_until - now
                logger.warning(
                    "🛑 Polza cooldown active. Waiting %.1fs...",
                    wait_time
                )
                await asyncio.sleep(wait_time)

            try:
                async with self._session.post(
                        url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120)
                ) as response:
                    body = await response.text()

                    if response.status == 200:
                        # Успех — сбрасываем счётчики
                        LLMPolza._global_cooldown_until = 0.0
                        LLMPolza._consecutive_429_count = 0

                        data = json.loads(body)
                        choices = data.get("choices", [])
                        if not choices:
                            return LLMResult(success=False, error_message="Empty choices")

                        finish_reason = choices[0].get("finish_reason")
                        content = choices[0].get("message", {}).get("content", "")

                        logger.info(
                            "✅ Polza response: finish_reason=%s, chars=%d",
                            finish_reason, len(content)
                        )

                        if finish_reason == "length":
                            logger.warning("⚠️ Response cut off by max_tokens!")

                        return LLMResult(success=True, content=content.strip())

                    # Обработка ошибок
                    if response.status == 429 or response.status >= 500:
                        if response.status == 429:
                            LLMPolza._consecutive_429_count += 1
                            logger.warning(
                                "⚠️ 429 received. Consecutive: %d",
                                LLMPolza._consecutive_429_count
                            )

                        # Активируем cooldown при 2+ подряд 429
                        if LLMPolza._consecutive_429_count >= 2:
                            cooldown_seconds = 60.0
                            LLMPolza._global_cooldown_until = time.time() + cooldown_seconds
                            logger.error(
                                "🚨 %d consecutive 429! Cooldown %.0fs.",
                                LLMPolza._consecutive_429_count, cooldown_seconds
                            )
                            return LLMResult(
                                success=False,
                                error_code=429,
                                error_message="Rate limit exceeded, cooldown activated"
                            )

                        # Задержка между ретраями
                        wait_time = 20.0
                        logger.warning(
                            "Polza API %s (attempt %d/%d). Waiting %.1fs...",
                            response.status, attempt + 1, max_retries, wait_time
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    # 400 (context too long)
                    if response.status == 400:
                        logger.error("Polza 400 error: %s", body[:500])
                        return LLMResult(
                            success=False,
                            error_code=400,
                            error_message="context_too_long"
                        )

                    # Прочие ошибки
                    logger.error("Polza error %s: %s", response.status, body[:500])
                    return LLMResult(
                        success=False,
                        error_code=response.status,
                        error_message=body[:500]
                    )

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Network error (attempt %d/%d): %s",
                    attempt + 1, max_retries, exc
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(20.0)
                    continue
                return LLMResult(success=False, error_code=0, error_message=str(exc))

        return LLMResult(
            success=False,
            error_code=0,
            error_message="Max retries exceeded"
        )