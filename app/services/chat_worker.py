from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
import random
from app.services.chat_queue import ChatTask
from app.services.llm import create_llm_client
from app.services.memory import maybe_generate_summary, build_llm_context
from app.db.repositories.payments import PaymentRepository  # ← НОВОЕ
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
        payment_repo: PaymentRepository,
) -> None:
    logger.info("🎯 START task: user=%s char=%s text='%s'",
                task.user_id, task.char_id, task.text[:50])

    is_start_message = task.text.startswith("[Начало диалога")

    # Проверка баланса перед генерацией
    if not is_start_message:
        balance = await payment_repo.get_user_balance(task.user_id)
        if balance <= 0:
            logger.warning("⚠️ User %s has no messages left", task.user_id)
            await api.send_message(
                peer_id=task.peer_id,
                text="😿 У тебя закончились сообщения! Купи новый пакет в главном меню."
            )
            return

    llm = create_llm_client(session)

    ERROR_MESSAGES = [
        "Ой, я немного задумалась... Попробуй написать ещё раз? 😊",
        "Хм, что-то меня отвлекло. Повтори, пожалуйста?",
        "Прости, я на секунду потеряла мысль. Что ты говорил?",
    ]

    answer = random.choice(ERROR_MESSAGES)
    is_real_answer = False  # ← ФЛАГ: был ли реальный ответ от LLM

    try:
        await maybe_generate_summary(
            session=session, llm=llm,
            msg_repo=msg_repo, summary_repo=summary_repo,
            user_id=task.user_id, character_id=task.char_id,
        )

        await asyncio.sleep(1.0)

        messages = await build_llm_context(
            msg_repo=msg_repo, summary_repo=summary_repo,
            user_id=task.user_id, character_id=task.char_id,
            system_prompt=task.char_dict["system_prompt"],
        )

        logger.info("📜 Last 3 messages in context:")
        for msg in messages[-3:]:
            logger.info("   [%s] %s", msg["role"], msg.get("content", "")[:100])

        result = await llm.generate(messages)

        if result.success:
            answer = _clean_response(result.content)
            answer = _truncate(answer)
            is_real_answer = True  # ← Реальный ответ получен
            logger.info("✅ Answer: '%s'", answer[:100])

            await msg_repo.add_message(
                task.user_id, task.char_id, "assistant", answer
            )
        else:
            logger.error(
                "❌ LLM failed: code=%s msg=%s",
                result.error_code, result.error_message
            )
            # answer остаётся заглушкой, is_real_answer = False
            # НЕ сохраняем заглушку в историю (чтобы не ломать RP)

    except Exception:
        logger.exception("💥 CRITICAL error in chat worker")
        # answer остаётся заглушкой, is_real_answer = False

    # Отправляем ответ пользователю
    try:
        # 🆕 Используем клавиатуру из задачи, если она передана
        await api.send_message(
            peer_id=task.peer_id,
            text=answer,
            keyboard=task.keyboard,
        )

        # Списываем одно сообщение после успешной отправки
        if not is_start_message and is_real_answer:
            success = await payment_repo.use_message(task.user_id)
            if success:
                new_balance = await payment_repo.get_user_balance(task.user_id)
                logger.info("💰 Energy used. User %s balance: %d",
                            task.user_id, new_balance)
            else:
                logger.warning("⚠️ Failed to use energy for user %s", task.user_id)
        elif not is_start_message and not is_real_answer:
            logger.info("💰 Energy NOT charged (LLM failed). User %s can retry",
                        task.user_id)

    except Exception:
        logger.exception("Failed to send message to user %s", task.user_id)

    logger.info("🏁 FINISHED task for user=%s", task.user_id)