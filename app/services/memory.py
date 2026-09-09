# app/services/memory.py
from __future__ import annotations

import logging
import re
import copy

from app.services.llm import LLMBase, create_llm_client
from app.db.repositories.messages import MessageRepository
from app.db.repositories.summaries import SummaryRepository
from app.config import get_llm_settings

logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ПАМЯТИ ===
SUMMARY_TRIGGER = 15          # Суммаризируем, когда накапливается 15 новых сообщений
SUMMARY_KEEP_LAST = 10        # При генерации саммари учитываем последние 10 сообщений как "новые"
HISTORY_WINDOW = 20           # 🔥 УВЕЛИЧЕНО до 20. Дает модели достаточно живого контекста, предотвращая потерь нити разговора.

SUMMARY_SYSTEM_PROMPT = """Ты — системный архивариус. Обновляй краткое резюме диалога.
ПРАВИЛА:
1. МЕСТО: Всегда начинай с текущей локации.
2. ФАКТЫ: Фиксируй новые важные факты, предметы, имена. Не удаляй старые, если они не опровергнуты.
3. ДИНАМИКА (КРИТИЧНО): Описывай, как ИЗМЕНИЛИСЬ отношения или действия с момента прошлого резюме. Если действие продолжается, пиши "Действие продолжается, но акцент сместился на...", не повторяй дословно прошлое описание. Избегай заморозки персонажа в одном состоянии.
4. СОСТОЯНИЯ: Описывай текущую эмоцию персонажа, но ИЗБЕГАЙ шаблонных зацикленных фраз ("щёки заливаются румянцем", "тихо стонет"). Используй разнообразные описания.
5. ФОРМАТ (строго):
- МЕСТО: [Локация]
- ДИНАМИКА: [1 предложение о том, что СЕЙЧАС происходит нового или как сместился фокус]
- ФАКТЫ: [Список ключевых фактов]
- СОБЫТИЕ: [Что произошло в последних 3-5 сообщениях, 1-2 предложения. Без воды.]
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
    summary_settings.max_tokens = 500

    summary_llm = create_llm_client(session)
    summary_llm._settings = summary_settings

    result = await summary_llm.generate(messages)
    if not result.success:
        return

    new_summary = result.content.strip()

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

    # 🔥 FIX: УДАЛЕНО вызов delete_old_messages.
    # История в БД сохраняется целиком. Это предотвращает "катастрофу потери контекста",
    # так как модель всегда может обратиться к полным данным, а HISTORY_WINDOW в build_llm_context
    # просто ограничивает размер промпта, не уничтожая данные.


async def build_llm_context(
    msg_repo: MessageRepository,
    summary_repo: SummaryRepository,
    user_id: int,
    character_id: int,
    system_prompt: str
) -> list[dict]:
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

    # Берем строго последние HISTORY_WINDOW сообщений ПОСЛЕ последнего саммари
    history = await msg_repo.get_recent_history(
        user_id, character_id,
        limit=HISTORY_WINDOW,
        after_message_id=last_summarized_id
    )

    # Простая дедупликация полных совпадений (защита от двойных отправок ВК)
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