from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.services.chat_queue import ChatTask
from app.services.llm import create_llm_client
from app.services.memory import maybe_generate_summary, build_llm_context
from app.vk.api import VKApi

logger = logging.getLogger(__name__)


def _truncate(value: str, limit: int = 1500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _clean_response(text: str) -> str:
    """Убирает английские вставки и мета-текст из ответа LLM."""
    replacements = {
        r'\bhandsome\b': 'красавчик', r'\bbaby\b': 'малыш',
        r'\bsweetheart\b': 'милый', r'\bhoney\b': 'солнце',
        r'\bdarling\b': 'дорогой', r'\bcute\b': 'милый',
        r'\bhey\b': 'привет', r'\bhi\b': 'привет',
        r'\bhello\b': 'привет', r'\bokay\b': 'хорошо',
        r'\bwow\b': 'вау', r'\bsorry\b': 'прости', r'\byeah\b': 'да',
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*Примечание[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*Note[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


async def process_chat_task(
    task: ChatTask,
    api: VKApi,
    session: Any,
    msg_repo: Any,
    summary_repo: Any,
) -> None:
    logger.info("🎯 START task: user=%s char=%s text='%s'",
                task.user_id, task.char_id, task.text[:50])

    # 🆕 Создаём клиент через фабрику
    llm = create_llm_client(session)

    # 🆕 Проверяем, стартовое ли это сообщение
    is_start_message = task.text.startswith("[Начало диалога")

    # Сохраняем в историю: либо реальный текст, либо маркер начала
    if is_start_message:
        # Сохраняем нейтральное сообщение от пользователя
        await msg_repo.add_message(
            task.user_id, task.char_id, "user", "(начал общение)"
        )
    answer = "Что-то у меня заискрило... Напиши ещё разочек? 🥺"

    try:
        # 1. Возможно, генерируем summary
        await maybe_generate_summary(
            session=session,
            llm=llm,
            msg_repo=msg_repo,
            summary_repo=summary_repo,
            user_id=task.user_id,
            character_id=task.char_id,
        )

        # Пауза между запросами (защита от 429)
        await asyncio.sleep(1.0)

        # 2. Собираем контекст
        messages = await build_llm_context(
            msg_repo=msg_repo,
            summary_repo=summary_repo,
            user_id=task.user_id,
            character_id=task.char_id,
            system_prompt=task.char_dict["system_prompt"],
        )

        # 3. Логируем последние 3 сообщения (для диагностики зацикливания)
        logger.info("📜 Last 3 messages in context:")
        for msg in messages[-3:]:
            logger.info("   [%s] %s", msg["role"], msg.get("content", "")[:100])

        # 4. Запрос к LLM
        result = await llm.generate(messages)

        if result.success:
            answer = _clean_response(result.content)
            answer = _truncate(answer)
            logger.info("✅ Answer: '%s'", answer[:100])

            # Сохраняем успешный ответ в историю
            await msg_repo.add_message(
                task.user_id, task.char_id, "assistant", answer
            )
        else:
            logger.error(
                "❌ LLM failed: code=%s msg=%s",
                result.error_code, result.error_message
            )
            # answer уже содержит дефолтное сообщение об ошибке
            # ВАЖНО: НЕ сохраняем ошибку в историю (чтобы не ломать RP)

    except Exception:
        logger.exception("💥 CRITICAL error in chat worker")
        # answer уже содержит дефолтное сообщение

    # 5. Всегда отправляем что-то пользователю
    try:
        await api.send_message(peer_id=task.peer_id, text=answer)
    except Exception:
        logger.exception("Failed to send message to user %s", task.user_id)

    logger.info("🏁 FINISHED task for user=%s", task.user_id)