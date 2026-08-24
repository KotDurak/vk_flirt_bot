from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
import random
from app.services.chat_queue import ChatTask
from app.services.llm import create_llm_client
from app.services.memory import maybe_generate_summary, build_llm_context
from app.db.repositories.payments import PaymentRepository
from app.vk.api import VKApi
from app.config import get_settings
logger = logging.getLogger(__name__)


def _truncate(value: str, limit: int = 1500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _clean_response(text: str) -> str:
    """Убирает английские вставки, мета-текст, системные теги и лишние абзацы из ответа LLM."""
    if not text:
        return ""

    # 1. УДАЛЕНИЕ СИСТЕМНЫХ ТЕГОВ И РАЗМЕТКИ
    # Убирает <MEMORY_CONTEXT>, </MEMORY_CONTEXT> и любые другие XML-подобные теги
    text = re.sub(r'<[^>]+>', '', text)
    # Убирает markdown-блоки кода, если модель вдруг решила оформить ответ как код
    text = re.sub(r'```(?:markdown|json|text)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'```', '', text)

    # 2. УДАЛЕНИЕ ЯВНЫХ УТЕЧЕК ПРОМПТА (Заголовки, правила)
    leak_patterns = [
        r'\+{2,}\s*диалог\.md.*',  # +++++ диалог.md
        r'\[.*?(?:ФОРМАТ|ПРАВИЛО|КОНТЕКСТ|СИСТЕМНАЯ|АНКЕТА|ГРАНИЦЫ).*?\]',  # [ФОРМАТ ОТВЕТА] и т.д.
        r'(?:Вот мой ответ|Как персонаж|Отыгрыш|Резюме):',  # Мета-вступления
    ]
    for pattern in leak_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)

    # 3. УДАЛЕНИЕ АНГЛИЙСКИХ ВСТАВОК (Ваш оригинальный код)
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

    # 4. 🚨 УМНАЯ ОЧИСТКА АБЗАЦЕВ
    # Берём первый абзац ТОЛЬКО если в нём нет прямой речи,
    # иначе обрежем реплику персонажа.
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

    if len(paragraphs) > 1:
        # Проверяем, есть ли прямая речь в первом абзаце
        # (тире, кавычки или текст после звёздочек)
        first_has_speech = bool(
            re.search(r'[—\-]"', paragraphs[0]) or
            re.search(r'\*[^*]+\*\s*\S', paragraphs[0])  # действие + текст после
        )

        if not first_has_speech:
            # В первом абзаце только действие — берём его + следующий (где речь)
            text = '\n\n'.join(paragraphs[:2])
        else:
            # В первом абзаце уже есть и действие, и речь — обрезаем мусор после
            text = paragraphs[0]

    # Если модель использовала одинарный перенос строки \n для разделения действий и речи,
    # мы заменяем его на пробел, чтобы всё было в одну строку, как в вашем примере.
    text = text.replace('\n', ' ')

    # 5. Финальная зачистка множественных пробелов
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

    is_start_message = task.text.startswith("[Начало диалога") or task.text.startswith("[СИСТЕМНАЯ КОМАНДА")

    # 🆕 ПРОВЕРКА: является ли эта задача регенерацией
    is_regeneration = getattr(task, 'is_regeneration', False)

    # Проверка баланса перед генерацией (ПРОПУСКАЕМ, если это регенерация, т.к. уже проверили и списали в handle_update)
    if not is_start_message and not is_regeneration:
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
            model_override=getattr(task, 'model_name', None)
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

        result = await llm.generate(messages, model_override=getattr(task, 'model_name', None))

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

    except Exception:
        logger.exception("💥 CRITICAL error in chat worker")

    # Отправляем ответ пользователю
    try:
        await api.send_message(
            peer_id=task.peer_id,
            text=answer,
            keyboard=getattr(task, 'keyboard', None),  # Используем клавиатуру из задачи
        )

        # 🆕 Списываем сообщение ТОЛЬКО если это не старт и НЕ регенерация
        if not is_start_message and is_real_answer and not is_regeneration and not get_settings().is_admin(task.user_dict['vk_user_id']):
            success = await payment_repo.use_message(task.user_id)
            if success:
                new_balance = await payment_repo.get_user_balance(task.user_id)
                logger.info("💰 Energy used. User %s balance: %d", task.user_id, new_balance)
            else:
                logger.warning("⚠️ Failed to use energy for user %s", task.user_id)

        elif not is_start_message and not is_real_answer and not is_regeneration:
            logger.info("💰 Energy NOT charged (LLM failed). User %s can retry", task.user_id)

        elif is_regeneration and is_real_answer:
            logger.info("♻️ Regeneration completed successfully. Energy was already deducted in handle_update.")

    except Exception:
        logger.exception("Failed to send message to user %s", task.user_id)

    logger.info("🏁 FINISHED task for user=%s", task.user_id)