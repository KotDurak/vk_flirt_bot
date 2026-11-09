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
SUMMARY_TRIGGER = 20          # Суммаризируем, когда накапливается 20 новых сообщений
HISTORY_WINDOW = 20           # Последние живые сообщения для контекста

SUMMARY_SYSTEM_PROMPT = """
Ты — Хранитель Памяти (Лор-мастер). Твоя задача — поддерживать актуальное, подробное, но сжатое резюме диалога.

ЖЁСТКИЕ ПРАВИЛА:
1. СТРУКТУРА: Используй блоки [CORE_FACTS] (фундамент) и [CURRENT_STATE] (текущий момент).
2. БЕЗ ИМЕН В РЕЗЮМЕ: В тексте резюме НЕ используй имена. Используй «Персонаж» и «Пользователь» (или опускай подлежащее). Имена уже зашиты в системном промпте.
3. ОБНОВЛЯЙ, А НЕ ДОПИСЫВАЙ (КРИТИЧНО!): Если статус отношений, локация или состояние изменились — ЗАМЕНИ старый текст на новый. Не оставляй в тексте историю изменений. Пиши только актуальное состояние.
4. СОХРАНЯЙ ДЕТАЛИ ЛОРА: Внешность, черты характера, манера физической близости — это важно для отыгрыша. Оставляй их в [CORE_FACTS], но формулируй тезисно, убирая "воду" и повторы.
5. ЛИМИТ: Общий объем резюме строго 600-800 слов. Если новые детали не влезают, безжалостно сжимай старые описания, но не теряй суть.

ФОРМАТ СТРОГО:

[CORE_FACTS]
- ОТНОШЕНИЯ: [Текущий статус в 1-2 предложениях. Напр: Официально встречаются. Персонаж доминирует, Пользователь влюблен и подчиняется.]
- ПЕРСОНАЖ (ЛОР): [Внешность, ключевые черты характера, стиль общения. Тезисно. Напр: Дерзкая скейтерша, платиновые волосы, пирсинг, хриплый голос. Любит подкалывать, требует действий, а не слов.]
- СТИЛЬ ВЗАИМОДЕЙСТВИЯ: [Как они взаимодействуют физически и эмоционально. Напр: Персонаж инициирует жесткий флирт, кусается, прижимает к себе. Пользователь нежен и готов подчиняться.]
- ПОЛЬЗОВАТЕЛЬ: [Ключевые черты. Напр: Влюблен, не умеет кататься на скейте, но старается ради Персонажа.]

[CURRENT_STATE]
- МЕСТО: [Где они находятся прямо сейчас]
- СОСТОЯНИЕ: [Текущие эмоции и атмосфера сцены]
- МИКРО-ФАКТЫ: [Что произошло в последних сообщениях. Только самое свежее! 3-4 тезиса.]

ПРИМЕР ПРАВИЛЬНОГО ОБНОВЛЕНИЯ:
Если в старом резюме было "Они флиртуют у моста", а в новых сообщениях они поцеловались и пошли за едой, ты НЕ пишешь: "Они флиртовали, потом поцеловались и пошли за едой".
Ты пишешь в [CURRENT_STATE]: "- МЕСТО: Путь к киоску с едой. - МИКРО-ФАКТЫ: После поцелуя Персонаж повел Пользователя за едой.
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
        from_message_id=last_summarized_id
    )

    if len(messages_for_summary) < SUMMARY_TRIGGER:
        return

    dialogue_text = ""
    if current_summary and current_summary["summary"]:
        dialogue_text += f"СТАРЫЙ БЛОК ПАМЯТИ (СОХРАНИ CORE_FACTS ИЗ НЕГО!):\n{current_summary['summary']}\n\n"

    dialogue_text += "НОВЫЕ СООБЩЕНИЯ ДЛЯ АНАЛИЗА:\n"
    for msg in messages_for_summary:
        role_label = "Пользователь" if msg["role"] == "user" else "Персонаж"
        dialogue_text += f"{role_label}: {msg['content']}\n"

    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": dialogue_text},
    ]

    summary_settings = copy.deepcopy(get_llm_settings())
    summary_settings.model = model_override if model_override else summary_settings.model_summary
    summary_settings.max_tokens = 4096
    # Немного понижаем temperature для суммаризации, чтобы она была более детерминированной и меньше "фантазировала"
    summary_settings.temperature = 0.2

    summary_llm = create_llm_client(session)
    summary_llm._settings = summary_settings

    result = await summary_llm.generate(messages)
    if not result.success:
        return

    new_summary = result.content.strip()

    # Валидация: проверяем наличие обоих критических заголовков
    if "[CORE_FACTS]" not in new_summary.upper() or "[CURRENT_STATE]" not in new_summary.upper():
        logger.warning("⚠️ Summary rejected: missing required sections [CORE_FACTS] or [CURRENT_STATE]")
        return

    cyrillic_chars = len(re.findall(r'[а-яА-ЯёЁ]', new_summary))
    if cyrillic_chars < 50 or len(new_summary) < 80:
        logger.warning("⚠️ Summary rejected: too short or no cyrillic")
        return

    last_message_id = messages_for_summary[-1]["id"]
    await summary_repo.save_summary(user_id, character_id, new_summary, last_message_id)
    logger.info(f"✅ Summary saved up to message {last_message_id}")


async def build_llm_context(
        msg_repo: MessageRepository,
        summary_repo: SummaryRepository,
        user_id: int,
        character_id: int,
        system_prompt: str,
        current_user_input: str | None = None  # <-- ДОБАВЛЕНО: явный триггер текущего действия
) -> list[dict]:
    system_content = system_prompt

    summary_data = await summary_repo.get_summary(user_id, character_id)

    # 1. Инъекция памяти (если есть)
    if summary_data and summary_data["summary"]:
        system_content += (
            "\n\n<MEMORY_CORE>\n"
            "АБСОЛЮТНЫЕ ИСТИНЫ (ВСЕГДА сверяй свой ответ с этим блоком. Игнорируй противоречия):\n"
            f"{summary_data['summary']}\n"
            "</MEMORY_CORE>"
        )

    messages = [{"role": "system", "content": system_content}]

    # 2. 🔥 FIX: БЕРЕМ АБСОЛЮТНО ПОСЛЕДНИЕ СООБЩЕНИЯ.
    # Убираем after_message_id, чтобы не обрезать живой контекст.
    # Нам нужны последние 20 сообщений ВООБЩЕ, а не "после саммари".
    history = await msg_repo.get_recent_history(
        user_id,
        character_id,
        limit=HISTORY_WINDOW
        # after_message_id УДАЛЕН
    )

    # 3. Дедупликация (защита от дублей)
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

    # 4. 🔥 SUPER FIX: Явный "Якорь текущего действия"
    # Если мы генерируем ответ на новое сообщение, добавляем его в самый конец
    # отдельным блоком, чтобы механизм Attention модели сфокусировался на нем на 100%.
    if current_user_input:
        messages.append({
            "role": "user",
            "content": f"\n[ТЕКУЩЕЕ ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ]: {current_user_input}\n(Сгенерируй реакцию персонажа, основываясь на MEMORY_CORE и последних сообщениях выше)"
        })

    return messages