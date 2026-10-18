from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import time

import aiohttp

from app.config import get_llm_settings
from app.services.llm.base import LLMBase, LLMResult

logger = logging.getLogger(__name__)


class LLMPolza(LLMBase):
    """
    Клиент для Polza.ai API (полностью OpenAI-совместимый).
    Включает защиту от коротких ответов, fallback и работу с изображениями.
    """

    _global_cooldown_until: float = 0.0
    _consecutive_429_count: int = 0

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._settings = get_llm_settings()
        # Polza использует стандартные переменные, но мы берем их из настроек
        self.base_url = getattr(self._settings, 'base_url', 'https://api.polza.ai/v1')
        self.api_key = getattr(self._settings, 'api_key', '')

    async def generate(
            self,
            messages: list[dict[str, str]],
            max_retries: int = 2,
            model_override: str | None = None,
            allow_short_response: bool = False,
    ) -> LLMResult:
        primary_model = model_override or getattr(self._settings, 'model', 'deepseek/deepseek-chat')
        fallback_model = getattr(self._settings, 'fallback_model', 'nousresearch/hermes-3-llama-3.1-70b')

        result = await self._try_generate(messages, primary_model, max_retries,
                                          allow_short_response=allow_short_response)

        # Если основная модель упала, пробуем fallback
        if not result.success and (result.error_code >= 400 or result.error_code in [0, 200]):
            logger.warning(
                f"⚠️ Primary model {primary_model} failed (code {result.error_code}). Switching to fallback: {fallback_model}")
            result = await self._try_generate(messages, fallback_model, max_retries, is_fallback=True,
                                              allow_short_response=allow_short_response)

        return result

    async def _try_generate(
            self,
            messages: list[dict[str, str]],
            target_model: str,
            max_retries: int,
            is_fallback: bool = False,
            allow_short_response: bool = False,
    ) -> LLMResult:
        prefix = "🔄 FALLBACK" if is_fallback else "🚀"
        logger.info(f"{prefix} Polza request: model=%s, msgs=%d", target_model, len(messages))

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": target_model,
            "messages": messages,
            "max_tokens": getattr(self._settings, 'max_tokens', 1000),
            "temperature": getattr(self._settings, 'temperature', 0.9),
            "top_p": 0.95,
            # Стандартные OpenAI-совместимые параметры.
            # Если Polza их не поддерживает, она их просто проигнорирует без ошибки.
            "frequency_penalty": 0.1,
            "presence_penalty": 0.05,
            "stop": ["User:", "Пользователь:", "[СИСТЕМА", "[SYSTEM"],
        }

        for attempt in range(max_retries):
            now = time.time()
            if now < LLMPolza._global_cooldown_until:
                wait_time = LLMPolza._global_cooldown_until - now
                logger.warning("🛑 Polza cooldown active. Waiting %.1fs...", wait_time)
                await asyncio.sleep(wait_time)

            try:
                async with self._session.post(
                        url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120)
                ) as response:
                    body = await response.text()

                    if response.status == 200:
                        data = json.loads(body)
                        choices = data.get("choices", [])
                        usage = data.get("usage", {})

                        if not choices:
                            logger.warning(f"⚠️ Empty choices! Full response: {body[:1000]}")
                            return LLMResult(success=False, error_code=200, error_message="Empty choices")

                        # КРИТИЧЕСКИЙ ФИКС: Защита от NoneType
                        raw_content = choices[0].get("message", {}).get("content")
                        content = raw_content if raw_content is not None else ""
                        finish_reason = choices[0].get("finish_reason")

                        # Защита от слишком коротких ответов (кроме задач выжимки)
                        if not allow_short_response and len(content.strip()) < 40:
                            logger.warning(
                                f"🚨 Ответ слишком короткий ({len(content.strip())} симв.). Форсируем fallback!")
                            return LLMResult(success=False, error_code=200, error_message="short_response")

                        result = LLMResult(success=True, content=content.strip())

                        if logger.isEnabledFor(logging.INFO):
                            self.log(messages, target_model, payload, content, usage)

                        logger.info("✅ Polza response: finish_reason=%s, chars=%d", finish_reason, len(content))
                        if finish_reason == "length":
                            logger.warning("⚠️ Response cut off by max_tokens!")

                        return result

                    if response.status == 429:
                        LLMPolza._consecutive_429_count += 1
                        logger.warning("⚠️ Polza 429 received. Consecutive: %d", LLMPolza._consecutive_429_count)

                        if LLMPolza._consecutive_429_count >= 2:
                            cooldown_seconds = 60.0
                            LLMPolza._global_cooldown_until = time.time() + cooldown_seconds
                            logger.error("🚨 %d consecutive 429 on Polza! Cooldown %.0fs.",
                                         LLMPolza._consecutive_429_count, cooldown_seconds)
                            return LLMResult(success=False, error_code=429, error_message="Rate limit exceeded")

                        wait_time = 15.0
                        logger.warning("Polza API 429 (attempt %d/%d). Waiting %.1fs...", attempt + 1, max_retries,
                                       wait_time)
                        await asyncio.sleep(wait_time)
                        continue

                    if response.status == 400:
                        logger.error("Polza 400 error: %s", body[:500])
                        return LLMResult(success=False, error_code=400, error_message="context_too_long")

                    logger.error("Polza error %s: %s", response.status, body[:500])
                    return LLMResult(success=False, error_code=response.status, error_message=body[:500])

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Polza Network error (attempt %d/%d): %s", attempt + 1, max_retries, exc)
                if attempt < max_retries - 1:
                    await asyncio.sleep(15.0)
                    continue
                return LLMResult(success=False, error_code=0, error_message=str(exc))

        return LLMResult(success=False, error_code=0, error_message="Max retries exceeded")

    async def analyze_image(
            self,
            image_url: str,
            prompt: str,
            model_override: str | None = None
    ) -> LLMResult:
        target_model = model_override or getattr(self._settings, 'model_vision', 'deepseek/deepseek-vision')
        logger.info(f"👁️ Polza Vision request: model={target_model}")

        vision_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }
        ]

        return await self._try_generate(
            messages=vision_messages,
            target_model=target_model,
            max_retries=2,
            is_fallback=False,
            allow_short_response=True,
        )

    async def generate_image(
            self,
            prompt: str,
            image_url: str | None = None,
            model_override: str | None = None
    ) -> dict | None:
        target_model = model_override or getattr(self._settings, 'image_model', 'bytedance/seedream-5-lite')
        logger.info(f"🎨 Polza Media API generation: model={target_model}, img2img={image_url is not None}")

        # 🔥 ПРАВИЛЬНЫЙ ЭНДПОИНТ: /v1/media (НЕ /images/generations!)
        url = f"{self.base_url.rstrip('/')}/media"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Формируем input для Media API
        input_params = {
            "prompt": prompt,
            "aspect_ratio": "1:1",  # Квадрат для ВК
        }

        #  ДОБАВЛЯЕМ ИЗОБРАЖЕНИЕ ДЛЯ IMG2IMG
        if image_url:
            # Определяем тип: http-ссылка или base64
            if image_url.startswith("http"):
                image_data_struct = {
                    "type": "url",
                    "data": image_url
                }
            else:
                # Base64 (data:image/...;base64,xxxx)
                image_data_struct = {
                    "type": "base64",
                    "data": image_url
                }

            input_params["images"] = [image_data_struct]
            logger.info("📎 Image reference added for img2img")

        payload = {
            "model": target_model,
            "input": input_params,
            "async": False  # Синхронный режим (ждем до 120 сек)
        }

        try:
            async with self._session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=130)
            ) as response:
                body_text = await response.text()

                if response.status in (200, 201):
                    try:
                        data = json.loads(body_text)
                    except json.JSONDecodeError:
                        logger.error(f"❌ Invalid JSON response: {body_text[:500]}")
                        return None

                    if "error" in data:
                        logger.error(f"❌ API returned error: {data['error']}")
                        return None

                    # Проверяем статус
                    status = data.get("status")

                    if status == "completed":
                        # Синхронный успех
                        media_data = data.get("data", {})
                        return await self._extract_image_from_media_response(media_data)

                    elif status == "pending":
                        # Асинхронный режим — поллинг
                        task_id = data.get("id")
                        logger.info(f"⏳ Async generation started. Task ID: {task_id}")
                        return await self._poll_media_status(task_id, headers)

                    else:
                        logger.error(f"❌ Unknown status: {status}. Full response: {body_text[:1000]}")
                        return None
                else:
                    logger.error(f"❌ Generation failed {response.status}: {body_text[:500]}")
                    return None

        except Exception as e:
            logger.error(f"💥 Network error: {e}")
            return None

    async def _extract_image_from_media_response(self, media_data: dict) -> dict | None:
        """Извлекает URL или base64 из ответа Media API."""
        if isinstance(media_data, dict) and "url" in media_data:
            logger.info("✅ Image generated (URL format)")
            return {"type": "url", "value": media_data["url"]}
        elif isinstance(media_data, list) and len(media_data) > 0:
            if "url" in media_data[0]:
                return {"type": "url", "value": media_data[0]["url"]}
            elif "b64_json" in media_data[0]:
                return await self._save_b64_image(media_data[0]["b64_json"],
                                                  media_data[0].get("media_type", "image/jpeg"))

        logger.error(f"❌ Cannot extract image from media_data: {media_data}")
        return None

    async def _poll_media_status(self, task_id: str, headers: dict) -> dict | None:
        """Опрашивает статус генерации через GET /v1/media/{id}."""
        status_url = f"{self.base_url.rstrip('/')}/media/{task_id}"

        max_attempts = 60  # 5 минут макс
        for attempt in range(max_attempts):
            await asyncio.sleep(5)

            try:
                async with self._session.get(status_url, headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        data = json.loads(await response.text())
                        status = data.get("status")

                        if status == "completed":
                            logger.info(f"✅ Async generation completed!")
                            return await self._extract_image_from_media_response(data.get("data", {}))
                        elif status == "failed":
                            logger.error(f"❌ Generation failed: {data.get('error')}")
                            return None
                        else:
                            logger.debug(f" Status: {status} (attempt {attempt + 1}/{max_attempts})")
                    else:
                        logger.warning(f"⚠️ Poll failed: {response.status}")
            except Exception as e:
                logger.warning(f"⚠️ Poll error: {e}")

        logger.error(f"❌ Polling timeout for task {task_id}")
        return None

    async def _save_b64_image(self, b64_data: str, media_type: str) -> dict:
        """Сохраняет base64 во временный файл."""
        import base64
        import tempfile
        import os

        logger.info("✅ Image generated (Base64 format), saving...")
        ext = media_type.split("/")[-1] if "/" in media_type else "jpeg"
        fd, temp_path = tempfile.mkstemp(suffix=f".{ext}")
        with os.fdopen(fd, 'wb') as f:
            f.write(base64.b64decode(b64_data))
        return {"type": "file", "value": temp_path}