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

# === НАСТРОЙКИ ПАМЯТИ (ЖЕСТКИЕ И ЭФФЕКТИВНЫЕ) ===
SUMMARY_TRIGGER = 15  # Суммаризируем чаще, чтобы история не росла
SUMMARY_KEEP_LAST = 10  # Оставляем 10 последних сообщений живыми
HISTORY_WINDOW = 15  # 🔥 ИСПРАВЛЕНО: 15 сообщений максимум. Доверяй саммари!

SUMMARY_SYSTEM_PROMPT = """Ты — системный архивариус. Обновляй краткое резюме диалога.
ПРАВИЛА:
1. МЕСТО: Всегда начинай с текущей локации.
2. ФАКТЫ: Фиксируй новые важные факты, предметы, имена, изменения в отношениях. Не удаляй старые, если не опровергнуты.
3. СОСТОЯНИЯ: Эмоции или ситуативные условия персонажа.
4. ФОРМАТ (строго):
- МЕСТО: [Локация]
- ДИНАМИКА: [1 предложение о развитии отношений]
- ФАКТЫ: [Список ключевых фактов]
- СОБЫТИЕ: [Что произошло в последних сообщениях, 1-2 предложения]
"""

async def maybe_generate_summary(session, llm: LLMBase, msg_repo: MessageRepository, summary_repo: SummaryRepository, user_id: int, character_id: int, model_override: str | None = None) -> None:
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

    dialogue_text += "Новые сообщения:\n"
    for msg in messages_for_summary:
        role_label = "Пользователь" if msg["role"] == "user" else "Персонаж"
        dialogue_text += f"{role_label}: {msg['content']}\n"

    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": dialogue_text},
    ]

    summary_settings = copy.deepcopy(get_llm_settings())
    summary_settings.model = model_override if model_override else summary_settings.model_summary
    summary_settings.max_tokens = 500 # Саммари не должно быть длинным

    summary_llm = create_llm_client(session)
    summary_llm._settings = summary_settings

    result = await summary_llm.generate(messages)
    if not result.success:
        return

    new_summary = result.content.strip()

    # 🔥 ИСПРАВЛЕНО: Мягкая, но надежная валидация
    cyrillic_chars = len(re.findall(r'[а-яА-ЯёЁ]', new_summary))
    if cyrillic_chars < 50 or len(new_summary) < 80:
        logger.warning("⚠️ Summary rejected: too short or no cyrillic")
        return

    if "МЕСТО:" not in new_summary.upper():
        logger.warning("⚠️ Summary rejected: missing location anchor")
        return

    last_message_id = messages_for_summary[-1]["id"]
    await summary_repo.save_summary(user_id, character_id, new_summary, last_message_id)
    logger.info(f"✅ Summary saved up to message {last_message_id}")

    # Чистим старые сообщения, оставляя только последние живые
    keep_messages = await msg_repo.get_recent_history(user_id, character_id, limit=SUMMARY_KEEP_LAST)
    if keep_messages:
        cutoff_id = keep_messages[0]["id"]
        await msg_repo.delete_old_messages(user_id, character_id, before_message_id=cutoff_id)
        logger.info(f"🧹 Cleaned old messages, kept last {SUMMARY_KEEP_LAST} alive")


async def build_llm_context(msg_repo: MessageRepository, summary_repo: SummaryRepository, user_id: int, character_id: int, system_prompt: str) -> list[dict]:
    system_content = system_prompt

    summary_data = await summary_repo.get_summary(user_id, character_id)
    last_summarized_id = summary_data["last_summarized_message_id"] if summary_data else 0

    if summary_data and summary_data["summary"]:
        system_content += (
            "\n\n<MEMORY_CONTEXT>\n"
            "КРАТКАЯ ВЫЖИМКА ПРОШЛЫХ СОБЫТИЙ (Используй для логики, не цитируй напрямую):\n"
            f"{summary_data['summary']}\n"
            "</MEMORY_CONTEXT>"
        )

    messages = [{"role": "system", "content": system_content}]

    # 🔥 ИСПРАВЛЕНО: Берем строго последние HISTORY_WINDOW сообщений ПОСЛЕ последнего саммари
    history = await msg_repo.get_recent_history(
        user_id, character_id,
        limit=HISTORY_WINDOW,
        after_message_id=last_summarized_id
    )

    # Простая дедупликация полных совпадений
    deduplicated = []
    for msg in history:
        if not deduplicated:
            deduplicated.append(msg)
            continue
        last_msg = deduplicated[-1]
        if last_msg.get("role") == msg.get("role") and last_msg.get("content") == msg.get("content"):
            continue
        deduplicated.append(msg)

    messages.extend(deduplicated)
    return messages