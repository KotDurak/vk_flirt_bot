from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import tempfile
from typing import Any

import aiohttp
import qrcode

logger = logging.getLogger(__name__)

# ... и сюда вставляешь сами функции ...

def _extract_message(update_object: dict[str, Any]) -> dict[str, Any]:
    message = update_object.get("message")
    if isinstance(message, dict):
        return message
    return update_object

def _truncate(value: str, limit: int = 1500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"

async def generate_and_upload_qr(api, data: str) -> str:
    """Генерирует QR-код, сохраняет во временный файл и загружает в VK через твой готовый метод"""
    try:
        # 1. Генерируем QR-код
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        # 2. Сохраняем во временный файл
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
            img.save(tmp_file, format='PNG')
            tmp_path = tmp_file.name

        try:
            # 3. 🎯 Используем ТВОЙ ЖЕ готовый метод upload_photo!
            attachment_string = await api.upload_photo(tmp_path)
            return attachment_string
        finally:
            # 4. Обязательно чистим за собой временный файл
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except Exception as e:
        logger.warning("⚠️ Не удалось загрузить QR-код в VK: %s", e)
        return ""  # Если не вышло, вернем пустоту (сработает запасной вариант)


def _clean_response(text: str) -> str:
    """Убирает английские вставки и мета-текст из ответа LLM."""
    import re

    replacements = {
        r'\bhandsome\b': 'красавчик',
        r'\bbaby\b': 'малыш',
        r'\bsweetheart\b': 'милый',
        r'\bhoney\b': 'солнце',
        r'\bdarling\b': 'дорогой',
        r'\bcute\b': 'милый',
        r'\bhey\b': 'привет',
        r'\bhi\b': 'привет',
        r'\bhello\b': 'привет',
        r'\bokay\b': 'хорошо',
        r'\bwow\b': 'вау',
        r'\bsorry\b': 'прости',
        r'\byeah\b': 'да',
    }

    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    text = re.sub(r'\([^)]*Примечание[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*Note[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()

    return text


async def shorten_url(long_url: str) -> str:
    """
    Сокращает ссылку через clck.ru (Яндекс).
    ВК гораздо лояльнее относится к коротким ссылкам и реже показывает предупреждения.
    """
    if not long_url:
        return ""
    try:
        async with aiohttp.ClientSession() as session:
            # clck.ru принимает исходный URL как параметр
            async with session.get(f"https://clck.ru/--?url={long_url}") as resp:
                if resp.status == 200:
                    short_url = await resp.text()
                    logger.info("🔗 URL shortened: %s -> %s", long_url, short_url.strip())
                    return short_url.strip()
    except Exception as e:
        logger.warning("⚠️ Failed to shorten URL: %s. Using original.", e)

    # Если сокращение не удалось, возвращаем оригинал
    return long_url