from __future__ import annotations

import logging
import json
import os
import base64
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

from app.config import get_settings

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
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    @abstractmethod
    async def generate(
            self,
            messages: list[dict[str, str]],
            max_retries: int = 2,
            model_override: str | None = None,
            allow_short_response: bool = False,
    ) -> LLMResult:
        """Генерирует текстовый ответ на основе списка сообщений."""
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

    async def analyze_image(
            self,
            image_url: str,
            prompt: str,
            model_override: str | None = None
    ) -> LLMResult:
        """
        Анализирует изображение.
        По умолчанию возвращает заглушку, если провайдер не поддерживает Vision.
        """
        return LLMResult(success=False, error_code=501, error_message="Vision not supported by this provider")


    async def generate_sd_forge(self, prompt: str) -> dict | None:
        """Отправляет задачу в SD Forge и возвращает task_id (не блокирует!)."""
        settings = get_settings()
        forge_url = getattr(settings, 'sd_forge_url', '').rstrip('/')
        forge_api_key = getattr(settings, 'sd_forge_api_key', '')

        if not forge_url:
            return None

        url = f"{forge_url}/sdapi/v1/txt2img"
        prompt_prefix = "masterpiece, best quality, amazing quality, 4k, very aesthetic, high resolution, ultra-detailed, absurdres, newest"
        prompt_suffix = "BREAK, depth of field, volumetric lighting"
        negative_suffix = (
            "modern, recent, old, oldest, cartoon, graphic, text, painting, crayon, graphite, abstract, glitch, "
            "deformed, mutated, ugly, disfigured, long body, lowres, bad anatomy, bad hands, missing fingers, "
            "extra digits, fewer digits, cropped, very displeasing, (worst quality, bad quality:1.2), "
            "bad anatomy, sketch, jpeg artifacts, signature, watermark, username, simple background, conjoined, "
            "bad ai-generated, "
            # 🔥 АНТИ-ЛОЛИ БЛОК (усиленный)
            "(loli:1.4), (shota:1.4), (child:1.4), (underage:1.4), minor, little girl, young girl, "
            "toddler, baby, kid, schoolgirl, schoolboy, elementary, middle school, "
            "(childish face:1.3), (baby face:1.3), (youthful face:1.3), (immature body:1.3), flat chest"
        )

        payload = {
            "sd_model_checkpoint": "novaAnimeXL_ilV190",
            "prompt": f"{prompt_prefix}, {prompt}, {prompt_suffix}",
            "negative_prompt": negative_suffix,
            "width": 832, "height": 1216, "steps": 20, "cfg_scale": 4.5,
            "sampler_name": "Euler a", "scheduler": "Karras", "clip_skip": 2,
            "batch_size": 1, "n_iter": 1,
        }

        try:
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30), headers={'x-api-key':forge_api_key}) as response:
                if response.status == 200:
                    data = await response.json()
                    # Если Forge вернул task_id — отлично, возвращаем его
                    if "task_id" in data:
                        logger.info(f"✅ Задача принята Forge. Task ID: {data['task_id']}")
                        return {"type": "task", "task_id": data["task_id"]}
                    # Если вдруг вернул картинку сразу (синхронно)
                    elif "images" in data and len(data["images"]) > 0:
                        return await self._save_b64_image(data["images"][0], "image/png")
                else:
                    logger.error(f"❌ Ошибка SD Forge API ({response.status})")
        except Exception as e:
            logger.error(f"💥 Ошибка при вызове SD Forge: {e}")
        return None