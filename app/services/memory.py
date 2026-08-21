from __future__ import annotations

import logging
from app.services.llm import LLMBase
from app.db.repositories.messages import MessageRepository
from app.db.repositories.summaries import SummaryRepository
from app.config import get_llm_settings
from app.services.llm import create_llm_client
import copy
import re
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# === 🆕 НАСТРОЙКИ ПАМЯТИ (Оптимизированы против зацикливания) ===
SUMMARY_TRIGGER = 6      # Суммаризируем каждые 6 сообщений (достаточно для контекста, не слишком часто)
SUMMARY_KEEP_LAST = 4    # После суммаризации оставляем 4 последних "сырых" сообщения для свежести
HISTORY_WINDOW = 6       # Загружаем ровно 6 сообщений после последней суммаризации

SUMMARY_SYSTEM_PROMPT = """Ты — архивариус диалога. Обнови резюме, сохраняя все важные факты.

СТРОГИЕ ПРАВИЛА (НАРУШЕНИЕ КАРАЕТСЯ):
1. ЗАПРЕЩЕНО ИНТЕРПРЕТИРОВАТЬ ПЕРСОНАЖЕЙ. Ты НЕ имеешь права давать оценки типа "девушка легкого поведения", "эскортница", "клиент", "содержанка", "проститутка". Описывай ТОЛЬКО факты: кто что сказал, кто что сделал.
2. ИСПОЛЬЗУЙ ТОЛЬКО ТЕРМИНЫ ИЗ СИСТЕМНОГО ПРОМПТА. Если персонаж назван "альтушкой", "манипуляторшей", "студенткой" — используй только эти слова. Никогда не придумывай новые ярлыки.
3. СОХРАНЯЙ КОНТЕКСТ: Если в старом резюме был важный факт (например, "они друзья", "он знает её секрет"), перенеси его в новое резюме.
4. ФОРМАТ:
   - СТАТУС: [Кем они являются друг другу сейчас: незнакомцы, знакомые, друзья].
   - МЕСТО: [Где они находятся прямо сейчас].
   - СОБЫТИЕ: [Что произошло в последних сообщениях, 1-2 предложения].
   - ФАКТЫ: [Имена, важные детали, о которых договорились].
5. Пиши сухо, в третьем лице. Максимум 4-5 предложений.
6. Если ты не уверена в каком-то факте — НЕ пиши его. Лучше пропустить, чем выдумать.
"""


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
    summary_settings = copy.deepcopy(get_llm_settings())
    summary_settings.model = summary_settings.model_summary
    summary_settings.max_tokens = 800

    summary_llm = create_llm_client(session)
    summary_llm._settings = summary_settings

    result = await summary_llm.generate(messages)

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
        return

    if not new_summary:
        logger.warning("LLM returned empty summary")
        return

    last_message_id = messages_for_summary[-1]["id"]

    await summary_repo.save_summary(
        user_id, character_id,
        new_summary, last_message_id
    )

    # 🆕 УДАЛЯЕМ старые сообщения из БД
    keep_messages = await msg_repo.get_recent_history(
        user_id, character_id,
        limit=SUMMARY_KEEP_LAST
    )
    if keep_messages:
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
            "\n\n[КОНТЕКСТ ПРОШЛЫХ БЕСЕД]:\n"
            f"{summary_data['summary']}\n"
            "Используй эту информацию для поддержания связности диалога. "
            "Если в резюме есть странные или противоречивые факты, игнорируй их и следуй системному промпту персонажа."
        )

    messages = [{"role": "system", "content": system_content}]

    # 🆕 Берём историю ТОЛЬКО после последней суммаризации
    history = await msg_repo.get_recent_history(
        user_id, character_id,
        limit=HISTORY_WINDOW,
        after_message_id=last_summarized_id
    )

    # 🆕 ИСПРАВЛЕННАЯ ДЕДУПЛИКАЦИЯ (сравниваем только роль и текст, игнорируя id)
    deduplicated = []
    for msg in history:
        if not deduplicated:
            deduplicated.append(msg)
            continue

        last_msg = deduplicated[-1]
        if last_msg.get("role") == msg.get("role") and last_msg.get("content") == msg.get("content"):
            continue  # Пропускаем дубликат

        deduplicated.append(msg)

    # 🆕 ПРОВЕРКА НА ДЕГРАДАЦИЮ (Вызов функций, которые были внизу файла)
    is_degraded, warning = _check_history_degradation(deduplicated)
    if is_degraded:
        system_content += warning
        # Аварийная обрезка: оставляем только последние 4 сообщения, чтобы "выбить" паттерн
        deduplicated = deduplicated[-4:]
        logger.warning("🔄 History trimmed to last 4 messages due to degradation detected")

    # Защита от переполнения
    MAX_HISTORY_CHARS = 32000  # 🆕 Увеличили лимит, чтобы не обрезало полезные сообщения
    current_chars = len(system_content)
    trimmed_history = []

    for msg in reversed(deduplicated):
        msg_len = len(str(msg.get("content", "")))  # str() на случай None
        if current_chars + msg_len > MAX_HISTORY_CHARS:
            break
        trimmed_history.insert(0, msg)
        current_chars += msg_len

    messages.extend(trimmed_history)
    return messages


# ==========================================
# 🆕 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ЗАЩИТЫ ОТ ЦИКЛОВ
# ==========================================

def _check_history_degradation(deduplicated: list[dict]) -> tuple[bool, str]:
    """
    Проверяет на опасное зацикливание и копипаст.
    """
    assistant_msgs = [m for m in deduplicated if m.get("role") == "assistant"]
    if len(assistant_msgs) < 2:
        return False, ""

    last_msg = assistant_msgs[-1]["content"]
    prev_msg = assistant_msgs[-2]["content"]

    # 1. ПРОВЕРКА НА ТЕКСТОВЫЙ КОПИПАСТ (Самая важная!)
    # Если сообщения длинные, проверяем их общее сходство
    if len(prev_msg) > 100 and len(last_msg) > 100:
        similarity = SequenceMatcher(None, last_msg, prev_msg).ratio()
        # Порог 0.45 ловит ситуации, когда модель копирует целые абзацы диалога
        if similarity > 0.45:
            logger.warning(f"🚨 TEXT COPYPASTE DETECTED! Similarity: {similarity:.2f}")
            warning_text = (
                "\n\n[КРИТИЧЕСКАЯ ОШИБКА СИСТЕМЫ: Ты дословно скопировала текст из предыдущего ответа!]\n"
                "НЕМЕДЛЕННО сгенерируй СОВЕРШЕННО НОВУЮ реплику, реагирующую на последние слова пользователя.\n"
                "Строго запрещено повторять фразы, предложения или абзацы из предыдущего сообщения."
            )
            return True, warning_text

    # 2. ПРОВЕРКА ДЕЙСТВИЙ (Звездочки)
    actions_last = set(re.findall(r'\*+(.*?)\*+', last_msg.lower()))
    actions_prev = set(re.findall(r'\*+(.*?)\*+', prev_msg.lower()))

    action_overlap = 0.0
    if actions_last and actions_prev:
        action_overlap = len(actions_last & actions_prev) / max(len(actions_last), len(actions_prev))

    # Если более 50% действий совпадают
    if action_overlap > 0.5:
        logger.warning(f"🚨 ACTION LOOP DETECTED! Overlap: {action_overlap:.2f}")
        warning_text = (
            "\n\n[СИСТЕМНОЕ ПРАВИЛО: Ты повторяешь одни и те же действия (*...*).]\n"
            "Немедленно используй новое микродвижение (вздохнуть, отстраниться, хмыкнуть, проверить телефон) "
            "и смени тон ответа. Запрещено использовать жесты из предыдущего сообщения."
        )
        return True, warning_text

    return False, ""