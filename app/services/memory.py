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
HISTORY_WINDOW = 12  # ⬆️ Было 8 → Стало 12 (разбавляем паттерны большим контекстом)
SUMMARY_TRIGGER = 12  # ⬇️ Было 20 → Стало 12 (суммаризируем чаще, чтобы не копился мусор)
SUMMARY_KEEP_LAST = 10  # ⬆️ Было 8 → Стало 10 (оставляем чуть больше контекста после чистки)

# === 🆕 ОБНОВЛЕННЫЙ ПРОМПТ ДЛЯ SUMMARY ===
SUMMARY_SYSTEM_PROMPT = """Ты — сухой, беспристрастный архивариус. Твоя задача — обновить фактологическое резюме диалога.

СТРОГИЕ ПРАВИЛА:
1. ИМЕНА: Используй ТОЛЬКО имена, которые явно указаны в диалоге. Если имя не упомянуто — НЕ выдумывай его. Используй "собеседник", "мужчина", "женщина" или описательные термины ("продавец", "девушка в красном").
2. СТРУКТУРА (строго следуй этому порядку):
   - ТЕКУЩАЯ ЛОКАЦИЯ: [Где они находятся прямо сейчас. Укажи, публичное это место или частное].
   - ТЕКУЩЕЕ ДЕЙСТВИЕ: [Что происходит ИМЕННО В ПОСЛЕДНЕМ сообщении, физический факт].
   - НЕДАВНИЕ СОБЫТИЯ: [1-2 факта о том, что было до этого, в хронологическом порядке].
3. СТИЛЬ: Телеграфный, протокольный, как сухой отчет.
4. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО:
   - Копировать фразы или имена из этого промпта. Анализируй именно предоставленный диалог.
   - Выдумывать имена, если они не указаны в диалоге.
   - Описывать эмоции, атмосферу, чувства. Только факты.
   - Использовать метафоры, эпитеты или художественные обороты.
   - Писать от первого лица ("мы", "я"). Пиши в третьем лице.
5. ОБЪЕМ: Максимум 3-4 коротких предложения.

ПРИМЕР ИДЕАЛЬНОГО РЕЗЮМЕ (для понимания формата, не копируй содержание!):
"ТЕКУЩАЯ ЛОКАЦИЯ: Городской парк на скамейке (публичное место).
ТЕКУЩЕЕ ДЕЙСТВИЕ: Девушка кормит уток, собеседник читает книгу.
НЕДАВНИЕ СОБЫТИЯ: Они встретились после работы, купили хлеб и пошли к пруду."
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
            "\n\n=== КРАТКОЕ РЕЗЮМЕ ПРОШЛЫХ БЕСЕД ===\n"
            f"{summary_data['summary']}"
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
    Проверяет на опасное зацикливание, сравнивая два последних ответа ассистента.
    Использует SequenceMatcher для выявления смысловых повторов и проверяет overlap действий.
    """
    assistant_msgs = [m for m in deduplicated if m.get("role") == "assistant"]

    # Нам нужно минимум 2 сообщения для сравнения
    if len(assistant_msgs) < 2:
        return False, ""

    last_msg = assistant_msgs[-1]["content"]
    prev_msg = assistant_msgs[-2]["content"]

    # 1. Проверка общей текстовой схожести (SequenceMatcher лучше Jaccard для парафразинга)
    text_similarity = SequenceMatcher(None, last_msg, prev_msg).ratio()

    # 2. Проверка повторения действий в звездочках (самая частая причина циклов в RP)
    actions_last = set(re.findall(r'\*+(.*?)\*+', last_msg.lower()))
    actions_prev = set(re.findall(r'\*+(.*?)\*+', prev_msg.lower()))

    if not actions_last or not actions_prev:
        action_overlap = 0.0
    else:
        action_overlap = len(actions_last & actions_prev) / max(len(actions_last), len(actions_prev))

    # 🚨 ПОРОГОВЫЕ ЗНАЧЕНИЯ: Если текст похож > 65% ИЛИ действия повторяются > 50%
    if text_similarity > 0.65 or action_overlap > 0.5:
        logger.warning(
            f"🚨 CRITICAL LOOP DETECTED! Text sim: {text_similarity:.2f}, Action overlap: {action_overlap:.2f}"
        )

        # АБСТРАКТНОЕ предупреждение. Никаких конкретных слов типа "улыбается"!
        warning_text = (
            "\n\n[ПРЕДУПРЕЖДЕНИЕ СИСТЕМЫ: ОБНАРУЖЕНО ПОВТОРЕНИЕ]\n"
            "Твои последние ответы слишком похожи по структуре и смыслу. Ты используешь одни и те же паттерны.\n"
            "ПРАВИЛО: Оставайся в рамках текущей темы и сцены, но РАЗВИВАЙ её, а не повторяй.\n"
            "Сделай одно из следующего:\n"
            "1. Углуби текущую мысль: задай уточняющий вопрос по теме разговора или вырази более конкретное мнение о ней.\n"
            "2. Добавь органичную микро-деталь: конкретное движение, соответствующее текущей позе (поправить воротник, перенести вес, изменить выражение взгляда), но НЕ используй жесты из предыдущего ответа.\n"
            "3. Свяжи текущий разговор с конкретной сенсорной деталью окружения (запах, текстура, конкретный предмет рядом).\n"
            "Запрещено резко менять тему или вводить неуместные внешние факторы. Ответь естественно и по-новому."
        )
        return True, warning_text

    return False, ""