# app/services/chat_worker.py
from __future__ import annotations

import asyncio
import logging
import re
import random
import copy
from typing import Any
from difflib import SequenceMatcher

from app.services.chat_queue import ChatTask
from app.services.llm import create_llm_client
from app.services.memory import maybe_generate_summary, build_llm_context
from app.db.repositories.payments import PaymentRepository
from app.vk.api import VKApi
from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_REGEN_ATTEMPTS = 2


def _truncate(value: str, limit: int = 1500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _is_duplicate_response(new_response: str, recent_assistant_msgs: list[str], last_user_msg: str = "") -> tuple[bool, list[str]]:
    """Детектор зацикливания: ловит копипаст и повторяющиеся действия."""
    if len(recent_assistant_msgs) < 2:
        return False, []

    last_msg = recent_assistant_msgs[-1]

    # 1. Полный копипаст
    if len(new_response) > 30 and len(last_msg) > 30:
        sim = SequenceMatcher(None, new_response, last_msg).ratio()
        if sim > 0.85:
            logger.warning(f"🚨 HARD COPYPASTE DETECTED (sim={sim:.2f})")
            return True, ["Полный копипаст последнего ответа"]

    # 2. Зацикливание действий
    def extract_actions(text: str) -> set[str]:
        actions = re.findall(r'\*([^*]+)\*', text.lower())
        return set(a.strip() for a in actions if len(a.strip()) > 8)

    new_actions = extract_actions(new_response)
    if new_actions:
        for old_msg in recent_assistant_msgs[-3:]:
            old_actions = extract_actions(old_msg)
            if old_actions and len(new_actions & old_actions) / len(new_actions) > 0.50:
                logger.warning(f"🚨 ACTION LOOP DETECTED: {new_actions & old_actions}")
                return True, ["Повторяющиеся действия в звездочках"]

    return False, []


def _clean_response(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r'\[.*?\]', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'```(?:markdown|json|text)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'```', '', text)

    leak_patterns = [
        r'\+{2,}\s*диалог\.md.*',
        r'(?:Вот мой ответ|Как персонаж|Отыгрыш|Резюме):',
    ]
    for pattern in leak_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)

    replacements = {
        r'\bhandsome\b': 'красавчик', r'\bbaby\b': 'малыш',
        r'\bsweetheart\b': 'милый', r'\bhoney\b': 'солнце',
        r'\bdarling\b': 'дорогой', r'\bcute\b': 'милый',
        r'\bhey\b': 'привет', r'\bhi\b': 'привет',
        r'\bhello\b': 'привет', r'\bokay\b': 'хорошо',
        r'\bwow\b': 'вау', r'\bsorry\b': 'прости', r'\byeah\b': 'да',
        r'\bvstupayet\b': 'входит', r'\bmaster\b': 'госпожа',
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    text = re.sub(r'\([^)]*(?:Примечание|Note|OOC|System)[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text)

    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

    if len(paragraphs) == 1 and len(paragraphs[0]) > 100:
        match = re.search(r'(\*[^*]{5,100}\*)\s*(—\s*.+)', paragraphs[0])
        if match:
            paragraphs = [match.group(1).strip(), match.group(2).strip()]

    clean_text = '\n\n'.join(paragraphs)
    lines = clean_text.split('\n\n')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in lines]

    return '\n\n'.join(lines).strip()


# ==============================================================================
# 🔥 ИЗОЛИРОВАННАЯ ОБРАБОТКА ФОТО (Single Responsibility Principle)
# ==============================================================================
async def _handle_photo_message(
    task: ChatTask,
    llm: Any,
    api: VKApi,
    msg_repo: Any,
    payment_repo: PaymentRepository,
) -> None:
    """Обрабатывает сообщение с фото через Vision API, сохраняя контекст диалога."""
    logger.info("📸 Processing photo for user %s", task.user_id)

    char_name = task.char_dict.get("name", "Персонаж")
    char_traits = task.char_dict.get("traits", "Добрый и отзывчивый персонаж.")

    # 🔥 ПОЛУЧАЕМ КОНТЕКСТ: Последние 3 сообщения для сохранения стиля
    recent_history = await msg_repo.get_recent_history(task.user_id, task.char_id, limit=3)
    context_text = ""
    if recent_history:
        context_lines = []
        for msg in recent_history:
            role = "Пользователь" if msg["role"] == "user" else char_name
            # Обрезаем длинные сообщения, чтобы не раздувать промпт
            content = msg.get("content", "")[:150]
            context_lines.append(f"{role}: {content}")
        context_text = "\n".join(context_lines)

    user_text_part = (
        f'И сказал при этом: "{task.text}"'
        if task.text.strip()
        else 'Пользователь не написал ничего, только прислал фото.'
    )

    vision_prompt = f"""Ты отыгрываешь персонажа: {char_name}.
Твои черты: {char_traits}

ПОСЛЕДНИЕ СООБЩЕНИЯ ДИАЛОГА (для сохранения стиля и контекста):
{context_text if context_text else "(Диалог только начался)"}

Пользователь прислал тебе фотографию.
{user_text_part}

Твоя задача: Опиши ЭМОЦИОНАЛЬНУЮ реакцию персонажа на это фото.
КРИТИЧЕСКИ ВАЖНО: Сохраняй ТОТ ЖЕ СТИЛЬ, тон и формат, что и в предыдущих сообщениях диалога выше.

СТРОГИЕ ПРАВИЛА ФОРМАТИРОВАНИЯ:
1. ДЕЙСТВИЯ: Всегда описывай действия в ТРЕТЬЕМ лице, используя имя персонажа (например: "*{char_name} прищуривается*", а НЕ "*Я прищуриваюсь*").
2. РЕЧЬ: Прямая речь идет от первого лица через тире (—).
3. Объем: Максимум 2-3 коротких предложения.
4. Детали: Добавь 1-2 сенсорные детали (что персонаж чувствует, видит, слышит).
5. Запрет: НИКОГДА не описывай фото как список объектов ("на фото изображен..."). Реагируй так, будто видишь это своими глазами.
6. Финал: Закончи вопросом или действием, передающим инициативу пользователю."""

    vision_result = await llm.analyze_image(
        image_url=task.photo_url,
        prompt=vision_prompt,
    )

    if vision_result.success and vision_result.content:
        candidate_answer = _clean_response(vision_result.content)
        candidate_answer = _truncate(candidate_answer)

        # 🔥 ПРОВЕРКА НА ПЬЯНСТВО
        is_drunk, drunk_reason = _is_drunk_response(candidate_answer)
        if is_drunk:
            logger.warning(f"🍸 Vision модель пьяна: {drunk_reason}. Используем фоллбэк.")
            answer = f"*{char_name} моргает, разглядывая картинку, и тепло улыбается.* Ох, как интересно! Расскажешь об этом подробнее?"
        else:
            answer = candidate_answer

        await msg_repo.add_message(task.user_id, task.char_id, "user", task.text or "[Отправил фото]")
        await msg_repo.add_message(task.user_id, task.char_id, "assistant", answer)

        await api.send_message(
            peer_id=task.peer_id,
            text=answer,
            keyboard=getattr(task, 'keyboard', None),
        )

        if not task.text.startswith("[СИСТЕМНАЯ КОМАНДА"):
            await payment_repo.use_message(task.user_id)

        logger.info("🏁 FINISHED photo task for user=%s", task.user_id)
    else:
        logger.error(f"❌ Vision API failed: {vision_result.error_message}")
        answer = f"*{char_name} прищуривается, пытаясь разглядеть детали.* Кажется, картинка не загрузилась, попробуй отправить ещё раз?"
        await api.send_message(peer_id=task.peer_id, text=answer)


# ==============================================================================
# ОСНОВНОЙ РАБОЧИЙ ПРОЦЕСС
# ==============================================================================
async def process_chat_task(
        task: ChatTask,
        api: VKApi,
        session: Any,
        msg_repo: Any,
        summary_repo: Any,
        payment_repo: PaymentRepository,
) -> None:
    logger.info("🎯 START task: user=%s char=%s text='%s'",
                task.user_id, task.char_id, task.text[:50])

    is_start_message = task.text.startswith("[Начало диалога") or task.text.startswith("[СИСТЕМНАЯ КОМАНДА")
    is_regeneration = getattr(task, 'is_regeneration', False)

    if not is_start_message and not is_regeneration:
        balance = await payment_repo.get_user_balance(task.user_id)
        if balance <= 0:
            logger.warning("⚠️ User %s has no messages left", task.user_id)
            await api.send_message(
                peer_id=task.peer_id,
                text="😿 У тебя закончились сообщения! Купи новый пакет в главном меню."
            )
            return

    llm = create_llm_client(session)

    # 🔥 ЧИСТАЯ МАРШРУТИЗАЦИЯ: Если есть фото, делегируем и выходим
    if getattr(task, 'photo_url', None):
        await _handle_photo_message(task, llm, api, msg_repo, payment_repo)
        return  # Завершаем задачу, фото обработано

    # ========================================================================
    # СТАНДАРТНАЯ ВЕТКА: ТЕКСТОВЫЙ ДИАЛОГ
    # ========================================================================
    ERROR_MESSAGES = [
        "Ой, я немного задумалась... Попробуй написать ещё раз? 😊",
        "Хм, что-то меня отвлекло. Повтори, пожалуйста?",
        "Прости, я на секунду потеряла мысль. Что ты говорил?",
    ]

    answer = random.choice(ERROR_MESSAGES)
    is_real_answer = False
    is_fallback = False
    candidate_answer = ""

    try:
        await maybe_generate_summary(
            session=session, llm=llm,
            msg_repo=msg_repo, summary_repo=summary_repo,
            user_id=task.user_id, character_id=task.char_id,
            model_override=getattr(task, 'model_name', None)
        )

        await asyncio.sleep(0.5)

        base_messages = await build_llm_context(
            msg_repo=msg_repo, summary_repo=summary_repo,
            user_id=task.user_id, character_id=task.char_id,
            system_prompt=task.char_dict["system_prompt"],
        )

        historical_assistant_msgs = [
            msg["content"] for msg in base_messages if msg.get("role") == "assistant"
        ]

        base_settings = getattr(llm, '_settings', None)
        base_temperature = getattr(base_settings, 'temperature', 0.8) if base_settings else 0.8

        for attempt in range(MAX_REGEN_ATTEMPTS + 1):
            messages_to_send = copy.deepcopy(base_messages)

            if attempt > 0 and base_settings is not None:
                increased_temp = min(base_temperature + 0.3, 1.2)
                logger.info(f"🌡️ Retry {attempt + 1}: Temp={increased_temp}")
                new_settings = copy.deepcopy(base_settings)
                new_settings.temperature = increased_temp
                llm._settings = new_settings

                messages_to_send.append({
                    "role": "system",
                    "content": "[OOC: Предыдущий ответ отклонен за структурное повторение. Сохраняя текущее настроение сцены и характер персонажа, опиши реакцию через новую, свежую деталь, избегая ранее использованных формулировок.]"
                })

            try:
                result = await llm.generate(messages_to_send, model_override=getattr(task, 'model_name', None))
            finally:
                if attempt > 0 and base_settings is not None:
                    llm._settings = base_settings

            if not result.success:
                logger.error("❌ LLM failed: code=%s msg=%s", result.error_code, result.error_message)
                break

            candidate_answer = _clean_response(result.content)
            candidate_answer = _truncate(candidate_answer)

            if not candidate_answer or not candidate_answer.strip():
                if attempt < MAX_REGEN_ATTEMPTS:
                    continue
                else:
                    char_name = task.char_dict.get("name", "Персонаж")
                    answer = f"*{char_name} задумчиво молчит, переводя взгляд на что-то новое вокруг.*"
                    is_real_answer = True
                    is_fallback = True
                    break

            # 🔥 ПРОВЕРКА НА ПЬЯНСТВО (Для текста)
            is_drunk, drunk_reason = _is_drunk_response(candidate_answer)
            if is_drunk:
                if attempt < MAX_REGEN_ATTEMPTS:
                    logger.warning(f"🍸 {drunk_reason}. Retrying with higher temperature...")
                    continue
                else:
                    logger.warning("🚨 FATAL DRUNK: Model is speaking gibberish/foreign. Using safe fallback.")
                    char_name = task.char_dict.get("name", "Персонаж")
                    answer = f"*{char_name} смущенно моргает, явно потеряв нить разговора, и мягко переводит тему, заглядывая тебе в глаза.*"
                    is_real_answer = True
                    is_fallback = True
                    break

            if _is_ai_refusal(candidate_answer):
                char_name = task.char_dict.get("name", "Персонаж")
                answer = f"*{char_name} делает паузу и мягко меняет тему, улыбнувшись*"
                is_real_answer = True
                is_fallback = True
                break

            is_dup, bad_phrases = _is_duplicate_response(candidate_answer, historical_assistant_msgs, task.text)

            if is_dup:
                if attempt < MAX_REGEN_ATTEMPTS:
                    logger.warning(f"🔄 Duplicate detected: {bad_phrases}. Retrying...")
                    continue
                else:
                    logger.warning("🚨 FATAL LOOP: Model stuck. Using safe fallback.")
                    char_name = task.char_dict.get("name", "Персонаж")
                    answer = f"*{char_name} мягко переводит тему, заглядывая тебе в глаза с новой эмоцией.*"
                    is_real_answer = True
                    is_fallback = True
                    break
            else:
                answer = candidate_answer
                is_real_answer = True
                logger.info("✅ Answer accepted (clean): '%s'", answer[:100])
                break

        if is_real_answer and not is_fallback:
            await msg_repo.add_message(task.user_id, task.char_id, "assistant", answer)
            logger.info("💾 Real answer saved to DB")
        elif is_fallback:
            logger.warning("⚠️ Fallback sent to user, but BLOCKED from DB (Context protected!)")

    except Exception:
        logger.exception("💥 CRITICAL error in chat worker")

    try:
        if not answer or not answer.strip():
            logger.error("🚨 CRITICAL SAFETY NET: Answer is empty right before VK API call! Forcing fallback.")
            answer = random.choice(ERROR_MESSAGES)
            is_real_answer = False

        await api.send_message(
            peer_id=task.peer_id,
            text=answer,
            keyboard=getattr(task, 'keyboard', None),
        )

        if not is_start_message and is_real_answer and not is_regeneration and not get_settings().is_admin(
                task.user_dict['vk_user_id']):
            success = await payment_repo.use_message(task.user_id)
            if success:
                new_balance = await payment_repo.get_user_balance(task.user_id)
                logger.info("💰 Energy used. User %s balance: %d", task.user_id, new_balance)
            else:
                logger.warning("⚠️ Failed to use energy for user %s", task.user_id)

        elif not is_start_message and not is_real_answer and not is_regeneration:
            logger.info("💰 Energy NOT charged (LLM failed). User %s can retry")

        elif is_regeneration and is_real_answer:
            logger.info("♻️ Regeneration completed successfully. Energy was already deducted in handle_update.")

    except Exception:
        logger.exception("Failed to send message to user %s", task.user_id)

    logger.info("🏁 FINISHED task for user=%s", task.user_id)


def _is_ai_refusal(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    hard_markers = [
        "языковая модель", "искусственный интеллект", "ИИ", "ai assistant",
        "политика использования", "правила безопасности",
        "не могу участвовать", "не могу предоставить", "не могу выполнить этот запрос",
        "извините, но я не могу", "я не могу помочь с этим"
    ]
    if any(marker in text_lower for marker in hard_markers):
        return True

    soft_markers = ["не могу продолжить этот разговор", "не могу выполнить этот запрос"]
    has_soft_marker = any(marker in text_lower for marker in soft_markers)
    has_roleplay_format = ("*" in text) or ("—" in text) or ("–" in text)

    if has_soft_marker and not has_roleplay_format:
        return True
    if has_soft_marker and len(text.split()) < 15:
        return True

    return False


def _is_drunk_response(text: str) -> tuple[bool, str]:
    """
    🔥 ИСПРАВЛЕННАЯ ВЕРСИЯ: Ловит мусор даже в коротких строках.
    """
    if not text or not text.strip():
        return False, ""

    alpha_count = len(re.findall(r'[a-zA-Zа-яА-ЯёЁ]', text))
    total_len = len(text.strip())

    # 🔥 ПРОВЕРКА НА МУСОР ПЕРВОЙ (даже для коротких строк!)
    if total_len > 5 and alpha_count < 3:
        return True, f"Пустой мусор (букв: {alpha_count}, длина: {total_len})"

    # Для остальных проверок нужна минимальная длина
    if total_len < 20:
        return False, ""

    cyrillic_count = len(re.findall(r'[а-яА-ЯёЁ]', text))

    if alpha_count > 25 and cyrillic_count == 0:
        return True, "Отсутствие кириллицы (DrunkSeek 3.2lv detected 🍸)"

    mojibake_chars = len(re.findall(r'[å½çļĦï¼Įè¯·ç¨įŃæĪŃ£ľ¨ä¸ºĤĩĨ¤ª²¾ĿĢķħĲİĬĮĸłÃÂ]', text))
    if mojibake_chars > 5:
        return True, "Обнаружена кодировочная каша (mojibake)"

    return False, ""