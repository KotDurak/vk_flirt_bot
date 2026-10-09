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
from app.config import get_settings, get_llm_settings

logger = logging.getLogger(__name__)

MAX_REGEN_ATTEMPTS = 2


# ==============================================================================
# 🔥 СПАСАТЕЛЬНЫЙ КРУГ: POLZA AI FALLBACK
# ==============================================================================
async def _rescue_with_polza(session, messages, settings) -> str | None:
    """Пытается получить ответ от Polza AI, чтобы перехватить ERROR_MESSAGES."""
    if not messages:
        return None

    # Защита: если ключи Polza не настроены, не пытаемся даже стучаться
    if not getattr(settings, 'polza_api_key', None):
        logger.warning("⚠️ Polza API key not configured. Skipping rescue.")
        return None

    try:
        from app.services.llm.routerai import LLMRouterAI

        # Создаем настройки для Polza на лету
        polza_settings = type('PolzaSettings', (), {
            'api_key': settings.polza_api_key,
            'base_url': settings.polza_base_url,
            'model': settings.polza_model,
            'max_tokens': settings.max_tokens,
            'temperature': settings.temperature
        })()

        polza_client = LLMRouterAI(session)

        # 🔥 КРИТИЧЕСКИЙ ФИКС: Перезаписываем атрибуты, которые __init__ захватил от RouterAI
        polza_client.base_url = settings.polza_base_url
        polza_client.api_key = settings.polza_api_key
        polza_client._settings = polza_settings

        # Копируем сообщения и добавляем строгий приказ не ломать роль
        messages_copy = copy.deepcopy(messages)
        messages_copy.append({
            "role": "system",
            "content": "[Instruction: Previous output failed or was truncated. CRITICAL: Stay strictly in character, do not break the fourth wall, continue the scene naturally in Russian.]"
        })

        result = await polza_client.generate(messages_copy)

        # Если Polza вернул нормальный текст — спасаем ситуацию
        if result.success and result.content and len(result.content.strip()) > 20:
            return _clean_response(_truncate(result.content))

    except Exception as e:
        logger.error(f"💥 Polza rescue failed: {e}")
    return None


def _truncate(value: str, limit: int = 1500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _is_duplicate_response(new_response: str, recent_assistant_msgs: list[str], last_user_msg: str = "") -> tuple[
    bool, list[str]]:
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
# 🔥 ИЗОЛИРОВАННАЯ ОБРАБОТКА ФОТО
# ==============================================================================
async def _handle_photo_message(
        task: ChatTask,
        llm: Any,
        api: VKApi,
        msg_repo: Any,
        payment_repo: PaymentRepository,
) -> None:
    logger.info("📸 Processing photo for user %s", task.user_id)

    char_name = task.char_dict.get("name", "Персонаж")
    char_traits = task.char_dict.get("traits", "Добрый и отзывчивый персонаж.")

    recent_history = await msg_repo.get_recent_history(task.user_id, task.char_id, limit=3)
    context_text = ""
    if recent_history:
        context_lines = []
        for msg in recent_history:
            role = "Пользователь" if msg["role"] == "user" else char_name
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
1. ДЕЙСТВИЯ: Всегда описывай действия в ТРЕТЬЕМ лице, используя имя персонажа.
2. РЕЧЬ: Прямая речь идет от первого лица через тире (—).
3. Объем: Максимум 2-3 коротких предложения.
4. Детали: Добавь 1-2 сенсорные детали.
5. Запрет: НИКОГДА не описывай фото как список объектов.
6. Финал: Закончи вопросом или действием, передающим инициативу пользователю."""

    vision_result = await llm.analyze_image(
        image_url=task.photo_url,
        prompt=vision_prompt,
    )

    if vision_result.success and vision_result.content:
        candidate_answer = _clean_response(vision_result.content)
        candidate_answer = _truncate(candidate_answer)

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

    if getattr(task, 'photo_url', None):
        await _handle_photo_message(task, llm, api, msg_repo, payment_repo)
        return

    # ========================================================================
    # СТАНДАРТНАЯ ВЕТКА: ТЕКСТОВЫЙ ДИАЛОГ
    # ========================================================================
    ERROR_MESSAGES = [
        "Ой, я немного задумалась... Попробуй написать ещё раз? 😊",
        "Хм, что-то меня отвлекло. Повтори, пожалуйста?",
        "Прости, я на секунду потеряла мысль. Что ты говорил?",
    ]

    answer = ""
    is_real_answer = False
    is_fallback = False
    candidate_answer = ""

    # Безопасная инициализация, чтобы избежать ошибок области видимости
    base_messages = []

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

    # ========================================================================
    # 🔥 ПОСЛЕДНИЙ РУБЕЖ ОБОРОНЫ: ГАРАНТИРОВАННЫЙ ОТВЕТ ПОЛЬЗОВАТЕЛЮ + ТЕСТ POLZA
    # ========================================================================
    try:
        # 🔥 ТЕСТОВАЯ ЗАГЛУШКА: Если админ пишет "тест полза", форсируем вызов Polza
        is_debug_force_polza = get_settings().is_admin(
            task.user_dict['vk_user_id']) and "тест полза" in task.text.lower()

        if not is_real_answer or is_debug_force_polza:
            if is_debug_force_polza:
                logger.info("🧪 [DEBUG] Принудительный вызов Polza AI по команде админа")
            else:
                logger.info("🚨 Основной провайдер упал. Пытаемся перехватить ERROR_MESSAGES через Polza...")

            polza_answer = await _rescue_with_polza(
                session,
                base_messages,
                get_llm_settings()
            )

            if polza_answer:
                answer = polza_answer
                is_real_answer = True
                logger.info("✅ Polza AI успешно перехватил фоллбэк!")
            else:
                logger.warning("💥 Polza AI тоже не смог. Возвращаем стандартный ERROR_MESSAGES.")
                if not answer or not answer.strip():
                    answer = random.choice(ERROR_MESSAGES)

        # 1. Если ответа всё ещё нет (или он пустой), генерируем безопасную ролевую заглушку.
        if not answer or not answer.strip():
            char_name = task.char_dict.get("name", "Персонаж")
            answer = f"*{char_name} на мгновение замерла, словно обдумывая твои слова, и мягко перевела тему, заглянув тебе в глаза.*"
            logger.warning("⚠️ SAFETY NET: Пустой ответ заменен на безопасную ролевую паузу.")

        # 2. Отправка с защитой от временных сбоев ВК (Error 10)
        max_send_attempts = 2
        for send_attempt in range(max_send_attempts):
            try:
                await api.send_message(
                    peer_id=int(task.peer_id),
                    text=answer,
                    keyboard=getattr(task, 'keyboard', None),
                    attachment=getattr(task, 'attachment', None),
                )
                break  # Успешно отправлено, выходим из цикла повторных попыток

            except Exception as vk_err:
                logger.error(f"🚨 VK Send attempt {send_attempt + 1} failed for user {task.user_id}: {vk_err}")
                if send_attempt == max_send_attempts - 1:
                    logger.critical(
                        f"💥 FAILED TO SEND MESSAGE TO USER {task.user_id} AFTER {max_send_attempts} ATTEMPTS")
                else:
                    await asyncio.sleep(2)  # Ждем 2 секунды перед повторной попыткой

        # 3. Логика списания энергии
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
        logger.exception("💥 CRITICAL: Полный крах при отправке сообщения пользователю %s", task.user_id)

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
    if not text or not text.strip():
        return False, ""

    text_stripped = text.strip()
    total_len = len(text_stripped)
    alpha_count = len(re.findall(r'[a-zA-Zа-яА-ЯёЁ]', text_stripped))
    cyrillic_count = len(re.findall(r'[а-яА-ЯёЁ]', text_stripped))

    # 🔥 Ловим артефакты токенизатора (Ġ = \u0120, Ċ = \u010a)
    tokenizer_artifacts = len(re.findall(r'[\u0120\u010a]', text_stripped))
    if tokenizer_artifacts > 2:
        return True, f"Обнаружен сбой токенизатора (Tokenizer leak): {tokenizer_artifacts} артефактов"

    if total_len > 5 and alpha_count < 3:
        return True, f"Пустой мусор (букв: {alpha_count}, длина: {total_len})"

    if total_len >= 20 and alpha_count > 25 and cyrillic_count == 0:
        return True, "Отсутствие кириллицы (DrunkSeek detected)"

    mojibake_chars = len(re.findall(r'[å½çļĦï¼Įè¯·ç¨įŃæĪŃ£ľ¨ä¸ºĤĩĨ¤ª²¾ĿĢķħĲİĬĮĸłÃÂ\u0120\u010a]', text_stripped))
    if mojibake_chars > 5:
        return True, "Обнаружена кодировочная каша (mojibake)"

    return False, ""