from __future__ import annotations

import logging
from app.services.llm import LLMBase
from app.db.repositories.messages import MessageRepository
from app.db.repositories.summaries import SummaryRepository
from app.config import get_llm_settings
from app.services.llm import create_llm_client
import re

logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ПАМЯТИ ===
HISTORY_WINDOW = 20
SUMMARY_TRIGGER = 25
SUMMARY_KEEP_LAST = 10

SUMMARY_SYSTEM_PROMPT = """Ты — ассистент, который сжимает историю диалога в краткое резюме.
Твоя задача: сохранить ключевые факты из беседы — имена, предпочтения, важные события, 
договоренности, эмоциональный тон и особенности отношений между собеседниками.
Не добавляй новых фактов, которых не было в диалоге.
Пиши на русском языке, кратко, в формате связного текста (до 150 слов)."""


async def maybe_generate_summary(
        session,
        llm: LLMBase,
        msg_repo: MessageRepository,
        summary_repo: SummaryRepository,
        user_id: int,
        character_id: int,
) -> None:
    """Генерирует summary, если накопилось достаточно новых сообщений."""
    current_summary = await summary_repo.get_summary(user_id, character_id)
    last_summarized_id = current_summary["last_summarized_message_id"] if current_summary else 0

    messages_for_summary = await msg_repo.get_messages_for_summary(
        user_id, character_id,
        from_message_id=last_summarized_id,
        keep_last=SUMMARY_KEEP_LAST
    )

    if len(messages_for_summary) < SUMMARY_TRIGGER:
        logger.debug(
            "Not enough messages for summary: %d < %d",
            len(messages_for_summary), SUMMARY_TRIGGER
        )
        return

    logger.info(
        "Generating summary for user_id=%d, character_id=%d, messages=%d",
        user_id, character_id, len(messages_for_summary)
    )

    # Формируем текст диалога для сжатия
    dialogue_text = ""
    if current_summary and current_summary["summary"]:
        dialogue_text += f"Предыдущее резюме:\n{current_summary['summary']}\n\n"

    dialogue_text += "Новые сообщения диалога:\n"
    for msg in messages_for_summary:
        role_label = "Пользователь" if msg["role"] == "user" else "Персонаж"
        dialogue_text += f"{role_label}: {msg['content']}\n"

    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": dialogue_text},
    ]

    # === СОЗДАЁМ ОТДЕЛЬНЫЙ КЛИЕНТ ДЛЯ SUMMARY (дешёвая модель) ===
    summary_settings = get_llm_settings()
    summary_settings.model = "sao10k/l3-lunaris-8b"  # ✅ Lunaris для русского
    summary_settings.max_tokens = 800

    summary_llm = create_llm_client(session)
    summary_llm._settings = summary_settings

    # ✅ ИСПРАВЛЕНО: вызываем summary_llm, а не llm
    result = await summary_llm.generate(messages)

    # ✅ ИСПРАВЛЕНО: проверяем LLMResult, а не строку
    if not result.success:
        logger.warning(
            "Failed to generate summary: error_code=%s msg=%s",
            result.error_code, result.error_message
        )
        return

    new_summary = result.content.strip()

    # 🆕 Проверяем, что summary на русском
    latin_chars = len(re.findall(r'[a-zA-Z]', new_summary))
    cyrillic_chars = len(re.findall(r'[а-яА-ЯёЁ]', new_summary))

    if latin_chars > cyrillic_chars:
        logger.warning(
            "⚠️ Summary is in English! Rejecting. Latin: %d, Cyrillic: %d",
            latin_chars, cyrillic_chars
        )
        return  # Не сохраняем английский summary

    if not new_summary:
        logger.warning("LLM returned empty summary")
        return

    # Определяем ID последнего сообщения
    last_message_id = messages_for_summary[-1]["id"]

    await summary_repo.save_summary(
        user_id, character_id,
        new_summary, last_message_id
    )

    # 🆕 УДАЛЯЕМ старые сообщения из БД (кроме последних SUMMARY_KEEP_LAST)
    # Находим ID сообщения, до которого нужно удалить
    keep_messages = await msg_repo.get_recent_history(
        user_id, character_id,
        limit=SUMMARY_KEEP_LAST
    )
    if keep_messages:
        # keep_messages отсортированы по возрастанию ID (после reversed в репозитории)
        # Берём ID самого старого из оставляемых
        cutoff_id = keep_messages[0]["id"]

        await msg_repo.delete_old_messages(
            user_id, character_id,
            before_message_id=cutoff_id
        )
        logger.info(
            "Deleted old messages before id=%d for user=%d char=%d",
            cutoff_id, user_id, character_id
        )

    logger.info(
        "Summary updated for user_id=%d, character_id=%d, up to message_id=%d",
        user_id, character_id, last_message_id
    )

    logger.info(
        "Summary updated for user_id=%d, character_id=%d, up to message_id=%d",
        user_id, character_id, last_message_id
    )


async def build_llm_context(
        msg_repo: MessageRepository,
        summary_repo: SummaryRepository,
        user_id: int,
        character_id: int,
        system_prompt: str,
) -> list[dict]:
    system_content = system_prompt

    summary_data = await summary_repo.get_summary(user_id, character_id)
    last_summarized_id = summary_data["last_summarized_message_id"] if summary_data else 0

    if summary_data and summary_data["summary"]:
        system_content += (
            "\n\n=== КРАТКОЕ РЕЗЮМЕ ПРОШЛЫХ БЕСЕД ===\n"
            f"{summary_data['summary']}"
        )

    messages = [{"role": "system", "content": system_content}]

    # 🆕 Берём историю ТОЛЬКО после последней суммаризации
    history = await msg_repo.get_recent_history(
        user_id, character_id,
        limit=HISTORY_WINDOW,
        after_message_id=last_summarized_id  # <-- НОВОЕ
    )

    # Дедупликация (уже было)
    deduplicated = []
    for msg in history:
        if deduplicated and deduplicated[-1] == msg:
            continue
        deduplicated.append(msg)

    # Защита от переполнения (уже было)
    MAX_HISTORY_CHARS = 16000
    current_chars = len(system_content)
    trimmed_history = []

    for msg in reversed(deduplicated):
        msg_len = len(msg.get("content", ""))
        if current_chars + msg_len > MAX_HISTORY_CHARS:
            break
        trimmed_history.insert(0, msg)
        current_chars += msg_len

    messages.extend(trimmed_history)
    return messages