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
SUMMARY_TRIGGER = 20  # Суммаризируем, когда накапливается 20 новых сообщений
HISTORY_WINDOW = 20  # Последние живые сообщения для контекста

SUMMARY_SYSTEM_PROMPT = """
Ты — Хранитель Памяти. Задача — сжатое, актуальное резюме диалога.

ЖЁСТКИЕ ПРАВИЛА:
1. СТРУКТУРА: Строго блоки [CORE_FACTS] и [CURRENT_STATE].
2. ОБНОВЛЯЙ, НЕ ДОПИСЫВАЙ: Если статус/локация изменились — ЗАМЕНИ старый текст новым. Историю изменений не веди.
3. НЕ ДУБЛИРУЙ АНКЕТУ (КРИТИЧНО ДЛЯ БЮДЖЕТА): В блоке "ПЕРСОНАЖ" пиши ТОЛЬКО то, что выявилось в диалоге и чего НЕТ в базовой карточке персонажа. Статичный лор НЕ ПЕРЕЧИСЛЯЙ. Если нового ничего нет — пиши "—".
4. ЗАПРЕТ НА РАССУЖДЕНИЯ: Категорически запрещено выводить процесс мышления, теги <think> или анализ задачи. Пиши только финальный результат.
5. ЛИМИТ: Строго 250–350 слов. Сжимай безжалостно.

ФОРМАТ:
[CORE_FACTS]
- ОТНОШЕНИЯ: [1–2 предложения. Текущий статус.]
- ПЕРСОНАЖ (НОВОЕ В ЛОРЕ): [ТОЛЬКО факты, которых нет в анкете. Если ничего — "—".]
- СТИЛЬ ВЗАИМОДЕЙСТВИЯ: [Как взаимодействуют СЕЙЧАС. 1–2 предложения.]
- ПОЛЬЗОВАТЕЛЬ: [Имя Пользователя + ключевые черты и текущее состояние. 1–2 предложения.]

[CURRENT_STATE]
- МЕСТО: [Где находятся]
- СОСТОЯНИЕ: [Эмоции и атмосфера]
- МИКРО-ФАКТЫ: [2–3 свежих тезиса о последних сообщениях]
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

    # FIX 1: Безопасный потолок. 350 слов = ~600 токенов. 2000 хватит с запасом, чтобы закончить формат,
    # но физически не даст улететь в 14 000 токенов в случае сбоя логики модели.
    summary_settings.max_tokens = 2000
    summary_settings.temperature = 0.2

    summary_llm = create_llm_client(session)
    summary_llm._settings = summary_settings

    result = await summary_llm.generate(messages)
    if not result.success:
        return

    new_summary = result.content.strip()

    # FIX 2: Безжалостная очистка от мусора и reasoning-тегов
    # 1. Вырезаем всё внутри тегов <think>...</think>
    new_summary = re.sub(r'<think>.*?</think>', '', new_summary, flags=re.DOTALL | re.IGNORECASE).strip()

    # 2. Если модель всё равно написала что-то до [CORE_FACTS], отрезаем всё до этого момента
    if "[CORE_FACTS]" in new_summary.upper():
        new_summary = new_summary[new_summary.upper().find("[CORE_FACTS]"):]

    # Валидация: проверяем наличие обоих критических заголовков
    if "[CORE_FACTS]" not in new_summary.upper() or "[CURRENT_STATE]" not in new_summary.upper():
        logger.warning("⚠️ Summary rejected: missing required sections [CORE_FACTS] or [CURRENT_STATE]")
        return

    # FIX 3: Проверка на перевес (защита от runaway генерации)
    # 2500 символов = ~400-450 слов. Если больше - модель проигнорировала лимит, мы не сохраняем этот мусор.
    if len(new_summary) > 2500:
        logger.warning(f"⚠️ Summary rejected: too long ({len(new_summary)} chars). Model ignored length limit.")
        return

    cyrillic_chars = len(re.findall(r'[а-яА-ЯёЁ]', new_summary))
    if cyrillic_chars < 50 or len(new_summary) < 80:
        logger.warning("⚠️ Summary rejected: too short or no cyrillic")
        return

    last_message_id = messages_for_summary[-1]["id"]
    await summary_repo.save_summary(user_id, character_id, new_summary, last_message_id)
    logger.info(f"✅ Summary saved up to message {last_message_id} (Length: {len(new_summary)} chars)")


async def build_llm_context(
        msg_repo: MessageRepository,
        summary_repo: SummaryRepository,
        user_id: int,
        character_id: int,
        system_prompt: str,
        current_user_input: str | None = None
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

    # 2. БЕРЕМ АБСОЛЮТНО ПОСЛЕДНИЕ СООБЩЕНИЯ.
    history = await msg_repo.get_recent_history(
        user_id,
        character_id,
        limit=HISTORY_WINDOW
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

    # 4. Явный "Якорь текущего действия"
    if current_user_input:
        messages.append({
            "role": "user",
            "content": f"\n[ТЕКУЩЕЕ ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ]: {current_user_input}\n(Сгенерируй реакцию персонажа, основываясь на MEMORY_CORE и последних сообщениях выше)"
        })

    return messages