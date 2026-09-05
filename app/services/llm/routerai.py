# app/services/llm/routerai.py
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

from app.config import get_llm_settings
from app.services.llm.base import LLMBase, LLMResult

logger = logging.getLogger(__name__)


class LLMRouterAI(LLMBase):
    """
    Клиент для RouterAI API (OpenAI-совместимый).
    Отлично подходит для Cydonia 24B и как fallback при 429 ошибках.
    """

    # Глобальные переменные для защиты от спама и rate limit
    _global_cooldown_until: float = 0.0
    _consecutive_429_count: int = 0

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._settings = get_llm_settings()

        # 🔥 PUSHOK FIX: Используем настройки RouterAI, если они есть в конфиге,
        # иначе берем дефолтные, но с правильным базовым URL
        self.base_url = getattr(self._settings, 'base_url', 'https://routerai.ru/api/v1')
        self.api_key = getattr(self._settings, 'api_key', self._settings.api_key)

    async def generate(
            self,
            messages: list[dict[str, str]],
            max_retries: int = 2,
            model_override: str | None = None
    ) -> LLMResult:
        # По умолчанию используем Cydonia на RouterAI, если не передано иное
        target_model = model_override or getattr(self._settings, 'routerai_model', 'thedrummer/cydonia-24b-v4.1')

        logger.info(
            "🚀 RouterAI request: model=%s, msgs=%d, tokens≈%d",
            target_model,
            len(messages),
            self._count_tokens_approx(messages)
        )

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": target_model,
            "messages": messages,
            "max_tokens": self._settings.max_tokens,
            "temperature": 0.85,  # Оставляем, отлично для креатива
            "top_p": 0.95,  # Чуть приподнимем, так как min_p сам отрежет хвосты
            "min_p": 0.05,  # 🔥 Оставляем! Отличный фильтр галлюцинаций.

            # --- ИСПРАВЛЕННАЯ ЗАЩИТА ОТ ПЕТЛЬ ---
            "repetition_penalty": 1.15,  # 🔥 Чуть повышаем (было 1.12).
            # Именно этот параметр (если бэкенд его поддерживает, например vLLM)
            # наказывает за повторение ПОДРЯД ИДУЩИХ n-грамм. Он спасает от
            # "Ты... ты правда?" и "*вздыхает* ... *вздыхает*".

            # --- УБИРАЕМ БЕНЗОПИЛУ ---
            "frequency_penalty": 0.1,  # 🔥 БЫЛО 1. Стало 0.1.
            # Легкий штраф только для ОЧЕНЬ частых слов-паразитов (типа "ну", "вот").

            "presence_penalty": 0.05,  # 🔥 БЫЛО 0.6. Стало 0.
            # В RP нам НЕ НУЖНО, чтобы бот постоянно менял тему.
            # Пусть спокойно продолжает описывать поцелуй или комнату!

            "stop": ["P.S", "@id", "User:", "Пользователь:", "[СИСТЕМА", "[SYSTEM"],
            # Совет: если бот иногда пишет за тебя, добавь в stop имя твоего персонажа с двоеточием (например, "Алексей:").

            "safe_prompt": False
        }

        for attempt in range(max_retries):
            now = time.time()
            if now < LLMRouterAI._global_cooldown_until:
                wait_time = LLMRouterAI._global_cooldown_until - now
                logger.warning("🛑 RouterAI cooldown active. Waiting %.1fs...", wait_time)
                await asyncio.sleep(wait_time)

            try:
                async with self._session.post(
                        url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120)  # 2 минуты на ответ
                ) as response:
                    body = await response.text()
                    if response.status == 200:
                        logger.debug(f"📥 ROUTERAI RAW RESPONSE:\n{body[:500]}")

                        data = json.loads(body)
                        choices = data.get("choices", [])
                        usage = data.get("usage", {})  # 🔥 Забираем статистику токенов

                        if not choices:
                            logger.warning(f"⚠️ Empty choices! Full response: {body[:1000]}")
                            if "error" in data:
                                logger.error(f"🚨 API Error in response: {data['error']}")
                            return LLMResult(
                                success=False,
                                error_code=200,
                                error_message="Empty choices - likely hit stop token or filtered"
                            )

                        # 🔥 ИЗВЛЕКАЕМ ДАННЫЕ ПЕРЕД ЛОГИРОВАНИЕМ
                        content = choices[0].get("message", {}).get("content", "")
                        finish_reason = choices[0].get("finish_reason")

                        # 1. Сначала создаем результат
                        result = LLMResult(success=True, content=content.strip())

                        # 2. Теперь безопасно логируем (передаем строку content и dict usage)
                        self.log(messages, target_model, payload, content, usage)

                        logger.info(
                            "✅ RouterAI response: finish_reason=%s, chars=%d",
                            finish_reason, len(content)
                        )

                        if finish_reason == "length":
                            logger.warning("⚠️ Response cut off by max_tokens!")

                        # 3. Возвращаем результат
                        return result

                    # Обработка Rate Limit (429)
                    if response.status == 429:
                        LLMRouterAI._consecutive_429_count += 1
                        logger.warning("⚠️ RouterAI 429 received. Consecutive: %d", LLMRouterAI._consecutive_429_count)

                        if LLMRouterAI._consecutive_429_count >= 2:
                            cooldown_seconds = 60.0
                            LLMRouterAI._global_cooldown_until = time.time() + cooldown_seconds
                            logger.error("🚨 %d consecutive 429 on RouterAI! Cooldown %.0fs.",
                                         LLMRouterAI._consecutive_429_count, cooldown_seconds)
                            return LLMResult(success=False, error_code=429,
                                             error_message="Rate limit exceeded, cooldown activated")

                        wait_time = 15.0  # Чуть меньше ждать, чем у Polza
                        logger.warning("RouterAI API 429 (attempt %d/%d). Waiting %.1fs...", attempt + 1, max_retries,
                                       wait_time)
                        await asyncio.sleep(wait_time)
                        continue

                    # Обработка ошибок контекста (400)
                    if response.status == 400:
                        logger.error("RouterAI 400 error (likely context too long): %s", body[:500])
                        return LLMResult(success=False, error_code=400, error_message="context_too_long")

                    # Прочие ошибки
                    logger.error("RouterAI error %s: %s", response.status, body[:500])
                    return LLMResult(success=False, error_code=response.status, error_message=body[:500])

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("RouterAI Network error (attempt %d/%d): %s", attempt + 1, max_retries, exc)
                if attempt < max_retries - 1:
                    await asyncio.sleep(15.0)
                    continue
                return LLMResult(success=False, error_code=0, error_message=str(exc))

        return LLMResult(success=False, error_code=0, error_message="Max retries exceeded")