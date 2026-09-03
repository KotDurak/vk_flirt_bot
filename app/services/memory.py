from __future__ import annotations

import logging
from app.services.llm import LLMBase
from app.db.repositories.messages import MessageRepository
from app.db.repositories.summaries import SummaryRepository
from app.config import get_llm_settings
from app.services.llm import create_llm_client
import copy
import re

logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ПАМЯТИ (СБАЛАНСИРОВАННЫЕ) ===
SUMMARY_TRIGGER = 20      # Суммаризируем каждые 20 сообщений (оптимально для RP)
SUMMARY_KEEP_LAST = 10    # Оставляем 10 последних сообщений живыми
HISTORY_WINDOW = 30       # Модель видит 30 последних сообщений

SUMMARY_SYSTEM_PROMPT = """Ты — системный архивариус. Твоя задача — обновлять краткое резюме диалога.

ПРАВИЛА:
1. МЕСТО: Если в диалоге произошла смена локации, ОБНОВИ это поле. Если нет — оставь прежнее.
2. ФАКТЫ: Добавляй новые важные факты о пользователе или мире к существующим. Не удаляй старые, если они не опровергнуты.
3. СОСТОЯНИЯ: Описывай текущие эмоции или ситуативные условия персонажа (например, "Даша нервничает", "Заказаны коктейли").
4. ФОРМАТ ВЫВОДА (строго, без вступлений):
- ДИНАМИКА: [1 предложение о развитии отношений].
- МЕСТО: [Текущая локация ПРЯМО СЕЙЧАС].
- ФАКТЫ: [Краткий список фактов о пользователе и мире].
- ТЕКУЩИЕ СОСТОЯНИЯ: [Активные условия].
- СОБЫТИЕ: [Что конкретно произошло в последних сообщениях, 1 предложение].
"""


async def maybe_generate_summary(
        session,
        llm: LLMBase,
        msg_repo: MessageRepository,
        summary_repo: SummaryRepository,
        user_id: int,
        character_id: int,
        model_override: str | None = None
) -> None:
    current_summary = await summary_repo.get_summary(user_id, character_id)
    last_summarized_id = current_summary["last_summarized_message_id"] if current_summary else 0

    messages_for_summary = await msg_repo.get_messages_for_summary(
        user_id, character_id,
        from_message_id=last_summarized_id,
        keep_last=SUMMARY_KEEP_LAST
    )

    if len(messages_for_summary) < SUMMARY_TRIGGER:
        return

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

    summary_settings = copy.deepcopy(get_llm_settings())
    summary_settings.model = model_override if model_override else summary_settings.model_summary
    summary_settings.max_tokens = 1000

    summary_llm = create_llm_client(session)
    summary_llm._settings = summary_settings

    result = await summary_llm.generate(messages)

    if not result.success:
        return

    new_summary = result.content.strip()

    latin_chars = len(re.findall(r'[a-zA-Z]', new_summary))
    cyrillic_chars = len(re.findall(r'[а-яА-ЯёЁ]', new_summary))

    if latin_chars > cyrillic_chars or not new_summary:
        logger.warning("⚠️ Summary rejected: garbage output or empty")
        return

    last_message_id = messages_for_summary[-1]["id"]
    await summary_repo.save_summary(user_id, character_id, new_summary, last_message_id)

    keep_messages = await msg_repo.get_recent_history(user_id, character_id, limit=SUMMARY_KEEP_LAST)
    if keep_messages:
        cutoff_id = keep_messages[0]["id"]
        await msg_repo.delete_old_messages(user_id, character_id, before_message_id=cutoff_id)


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

    # 1. Добавляем саммари, если оно есть
    if summary_data and summary_data["summary"]:
        system_content += (
            "\n\n<MEMORY_CONTEXT>\n"
            "КРАТКАЯ ВЫЖИМКА ПРОШЛЫХ СОБЫТИЙ (Используй для логики, не цитируй):\n"
            f"{summary_data['summary']}\n"
            "</MEMORY_CONTEXT>"
        )

    messages = [{"role": "system", "content": system_content}]

    # 2. Загружаем историю
    history = await msg_repo.get_recent_history(
        user_id, character_id,
        limit=HISTORY_WINDOW,
        after_message_id=last_summarized_id
    )

    # 3. Простая дедупликация (убираем только полные дубликаты подряд)
    deduplicated = []
    for msg in history:
        if not deduplicated:
            deduplicated.append(msg)
            continue
        last_msg = deduplicated[-1]
        if last_msg.get("role") == msg.get("role") and last_msg.get("content") == msg.get("content"):
            continue
        deduplicated.append(msg)

    # 4. Ограничение по символам (защита от переполнения контекста)
    MAX_HISTORY_CHARS = 32000
    current_chars = len(system_content)
    trimmed_history = []

    for msg in reversed(deduplicated):
        msg_len = len(str(msg.get("content", "")))
        if current_chars + msg_len > MAX_HISTORY_CHARS:
            break
        trimmed_history.insert(0, msg)
        current_chars += msg_len

    messages.extend(trimmed_history)

    # 🚨 ЗДЕСЬ БЫЛ УДАЛЕН ВЕСЬ БЛОК _check_history_degradation И КРИТИЧЕСКИЙ ЯКОРЬ ЛОКАЦИИ.
    # Они вызывали противоречия и отказы модели. Контекст теперь чистый.

    return messages