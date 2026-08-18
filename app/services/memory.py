from __future__ import annotations

import logging
from app.services.llm import LLMBase
from app.db.repositories.messages import MessageRepository
from app.db.repositories.summaries import SummaryRepository
from app.config import get_llm_settings
from app.services.llm import create_llm_client
import re
import copy

logger = logging.getLogger(__name__)

# === 🆕 НАСТРОЙКИ ПАМЯТИ (Оптимизированы против зацикливания) ===
HISTORY_WINDOW = 12  # ⬆️ Было 8 → Стало 12 (разбавляем паттерны большим контекстом)
SUMMARY_TRIGGER = 12  # ⬇️ Было 20 → Стало 12 (суммаризируем чаще, чтобы не копился мусор)
SUMMARY_KEEP_LAST = 10  # ⬆️ Было 8 → Стало 10 (оставляем чуть больше контекста после чистки)

# === 🆕 ОБНОВЛЕННЫЙ ПРОМПТ ДЛЯ SUMMARY ===
SUMMARY_SYSTEM_PROMPT = """Ты — ассистент, который обновляет краткое резюме диалога.
Твоя задача:
1. Сохрани ключевые факты из предыдущего резюме (если оно есть).
2. Добавь новые факты из последних сообщений.
3. ВАЖНО: Не повторяй формулировки и структуры из предыдущего резюме. Перефразируй, используй другие слова и глаголы.
4. Избегай шаблонных фраз вроде "пользователь спросил", "персонаж ответил". Пиши живо и разнообразно.
5. Пиши на русском языке, кратко, в формате связного текста (до 150-200 слов)."""


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

def _calculate_similarity(text1: str, text2: str) -> float:
    """Простая проверка похожести по общим словам (Jaccard)."""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    if not words1 or not words2:
        return 0.0
    intersection = words1 & words2
    union = words1 | words2
    return len(intersection) / len(union)


def _check_structural_similarity(text1: str, text2: str) -> float:
    """Проверяет структурное сходство (одинаковые фразы в начале/конце)."""
    # Берем первые 30 и последние 30 символов
    prefix1 = text1[:30].lower().strip()
    prefix2 = text2[:30].lower().strip()
    suffix1 = text1[-30:].lower().strip()
    suffix2 = text2[-30:].lower().strip()

    # Проверяем, начинаются ли ответы одинаково
    prefix_match = prefix1 == prefix2

    # Проверяем, заканчиваются ли ответы одинаково
    suffix_match = suffix1 == suffix2

    # Если и начало, и конец одинаковые — это структурный повтор
    if prefix_match and suffix_match:
        return 1.0
    elif prefix_match or suffix_match:
        return 0.7

    return 0.0


def _check_history_degradation(deduplicated: list[dict]) -> tuple[bool, str]:
    """Проверяет, не скатилась ли история в паттерн."""
    assistant_msgs = [m for m in deduplicated if m.get("role") == "assistant"]

    if len(assistant_msgs) < 3:
        return False, ""

    # Берём последние 3 ответа ассистента
    last_3 = assistant_msgs[-3:]

    # Проверяем попарное сходство (и по словам, и по структуре)
    word_similarities = [
        _calculate_similarity(last_3[0]["content"], last_3[1]["content"]),
        _calculate_similarity(last_3[1]["content"], last_3[2]["content"]),
        _calculate_similarity(last_3[0]["content"], last_3[2]["content"]),
    ]

    structural_similarities = [
        _check_structural_similarity(last_3[0]["content"], last_3[1]["content"]),
        _check_structural_similarity(last_3[1]["content"], last_3[2]["content"]),
        _check_structural_similarity(last_3[0]["content"], last_3[2]["content"]),
    ]

    avg_word_sim = sum(word_similarities) / len(word_similarities)
    avg_struct_sim = sum(structural_similarities) / len(structural_similarities)

    # Если средняя похожесть по словам > 0.5 ИЛИ структурная > 0.7 — это деградация
    if avg_word_sim > 0.5 or avg_struct_sim > 0.7:
        logger.warning(
            "🚨 Degradation detected! Word similarity: %.2f, Structural: %.2f",
            avg_word_sim, avg_struct_sim
        )
        return True, (
            "\n\n[СРОЧНОЕ ПРЕДУПРЕЖДЕНИЕ СИСТЕМЫ]\n"
            "Твои последние ответы стали слишком похожи друг на друга по структуре.\n"
            "НЕМЕДЛЕННО кардинально смени стиль, структуру и действия.\n"
            "Если собеседник задал вопрос — ОБЯЗАНА ответить на него напрямую.\n"
            "Не используй фразы '*улыбаясь* Ох, *смеюсь*' или '**Прижимаюсь к тебе**' — они повторяются слишком часто.\n"
            "Начни ответ с действия, которого ещё не было в этом диалоге."
        )

    return False, ""