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

# === НАСТРОЙКИ ПАМЯТИ (ОПТИМИЗИРОВАННЫЕ ДЛЯ RP) ===
SUMMARY_TRIGGER = 20  # Суммаризируем каждые 20 сообщений
SUMMARY_KEEP_LAST = 15  # 🔥 PUSHOK FIX: Оставляем 15 последних сообщений живыми (было 10)
HISTORY_WINDOW = 40  # 🔥 PUSHOK FIX: Модель видит 40 последних сообщений (было 30)

SUMMARY_SYSTEM_PROMPT = """Ты — системный архивариус. Твоя задача — обновлять краткое резюме диалога.

ПРАВИЛА:
1. МЕСТО (ПЕРВЫМ ДЕЛОМ): Всегда начинай резюме с явного указания текущей локации. Если локация не изменилась, повтори предыдущую.
2. ФАКТЫ: Добавляй новые важные факты о пользователе или мире. ОБЯЗАТЕЛЬНО фиксируй конкретные предметы, одежду, подарки, имена. Не удаляй старые факты, если они не опровергнуты.
3. СОСТОЯНИЯ: Описывай текущие эмоции или ситуативные условия персонажа.
4. ЗАПРЕТ НА МИКРО-ПОВТОРЫ: НЕ пиши о повторяющихся мелких действиях (например, "постоянно смотрит в телефон"). Пиши только о глобальных изменениях.
5. ФОРМАТ ВЫВОДА (строго, без вступлений):
- МЕСТО: [Текущая локация ПРЯМО СЕЙЧАС — это первое поле!]
- ДИНАМИКА: [1 предложение о развитии отношений].
- ФАКТЫ: [Краткий список фактов о пользователе и мире, включая конкретные предметы и одежду].
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

    # 🔥 PUSHOK FIX: Проверка качества саммари
    if len(new_summary) < 100:
        logger.warning("⚠️ Summary rejected: too short (less than 100 chars)")
        return

    if "МЕСТО:" not in new_summary.upper():
        logger.warning("⚠️ Summary rejected: missing location anchor")
        return

    # Проверяем, что саммари содержит конкретные факты
    fact_keywords = ["носит", "одежд", "подар", "имя", "зовут", "любит", "предпочит"]
    has_concrete_facts = any(keyword in new_summary.lower() for keyword in fact_keywords)
    if not has_concrete_facts and len(new_summary) < 300:
        logger.warning("⚠️ Summary rejected: too abstract, no concrete facts")
        return

    last_message_id = messages_for_summary[-1]["id"]
    await summary_repo.save_summary(user_id, character_id, new_summary, last_message_id)

    # 🔥 PUSHOK FIX: Удаляем только очень старые сообщения, оставляя больше живых
    keep_messages = await msg_repo.get_recent_history(user_id, character_id, limit=20)
    if keep_messages:
        cutoff_id = keep_messages[0]["id"]
        await msg_repo.delete_old_messages(user_id, character_id, before_message_id=cutoff_id)
        logger.info(f"🧹 Cleaned old messages, kept last 20 alive")


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

    return messages