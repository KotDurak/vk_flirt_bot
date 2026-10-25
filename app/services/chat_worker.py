# app/services/chat_worker.py
from __future__ import annotations
import asyncio
import logging
import random
from typing import Any

from app.services.chat_queue import ChatTask
from app.services.llm import create_llm_client
from app.services.memory import maybe_generate_summary, build_llm_context
from app.db.repositories.payments import PaymentRepository
from app.db.repositories.generations import GenerationRepository
from app.vk.api import VKApi
from app.config import get_settings, get_llm_settings

# Импортируем вынесенные модули
from app.services.chat_utils import (
    clean_response, truncate, is_duplicate_response,
    is_ai_refusal, is_drunk_response, rescue_with_polza
)
from app.services.image_generator import handle_image_generation

logger = logging.getLogger(__name__)
MAX_REGEN_ATTEMPTS = 2


async def handle_photo_message(
        task: ChatTask, llm: Any, api: VKApi, msg_repo: Any, payment_repo: PaymentRepository
) -> None:
    """Краткая обработка фото (можно тоже вынести в vision_worker.py позже, но пока оставим тут для простоты)"""
    logger.info("📸 Processing photo for user %s", task.user_id)
    is_support = task.char_dict.get("is_support", False)
    char_name = task.char_dict.get("name", "Персонаж")

    recent_history = await msg_repo.get_recent_history(task.user_id, task.char_id, limit=3)
    context_text = "\n".join(
        [f"{'Пользователь' if msg['role'] == 'user' else char_name}: {msg.get('content', '')[:150]}" for msg in
         recent_history]) if recent_history else "(Диалог только начался)"

    vision_prompt = f"Ты отыгрываешь персонажа: {char_name}. Твои черты: {task.char_dict.get('traits', 'Добрый')}. КОНТЕКСТ: {context_text}. Пользователь прислал фото и сказал: '{task.text}'. Опиши ЭМОЦИОНАЛЬНУЮ реакцию. Правила: 1. Действия в 3-м лице (*). 2. Речь от 1-го лица (—). 3. Макс 2-3 предложения. 4. Добавь сенсорные детали. 5. Не описывай фото как список."

    vision_result = await llm.analyze_image(image_url=task.photo_url, prompt=vision_prompt)
    if vision_result.success and vision_result.content:
        candidate_answer = truncate(clean_response(vision_result.content))
        is_drunk, _ = is_drunk_response(candidate_answer)
        answer = f"*{char_name} моргает, разглядывая картинку, и тепло улыбается.* Ох, как интересно!" if is_drunk else candidate_answer

        await msg_repo.add_message(task.user_id, task.char_id, "user", task.text or "[Отправил фото]")
        await msg_repo.add_message(task.user_id, task.char_id, "assistant", answer)
        await api.send_message(peer_id=task.peer_id, text=answer, keyboard=getattr(task, 'keyboard', None))
        if not task.text.startswith("[СИСТЕМНАЯ КОМАНДА") and not is_support:
            await payment_repo.use_message(task.user_id)
    else:
        await api.send_message(peer_id=task.peer_id,
                               text=f"*{char_name} прищуривается.* Кажется, картинка не загрузилась.")


async def process_chat_task(
        task: ChatTask, api: VKApi, session: Any, msg_repo: Any, summary_repo: Any,
        payment_repo: PaymentRepository, gen_repo: GenerationRepository
) -> None:
    logger.info("🚀 START task: user=%s char=%s", task.user_id, task.char_id)

    is_start_message = task.text.startswith("[Начало диалога") or task.text.startswith("[СИСТЕМНАЯ КОМАНДА")
    is_regeneration = getattr(task, 'is_regeneration', False)
    is_support = task.char_dict.get("is_support", False)

    if not is_start_message and not is_regeneration and not is_support:
        if await payment_repo.get_user_balance(task.user_id) <= 0:
            await api.send_message(peer_id=task.peer_id,
                                   text="😿 У тебя закончились сообщения! Купи новый пакет в главном меню.")
            return

    llm = create_llm_client(session)

    # Маршрутизация задач
    if getattr(task, 'photo_url', None):
        return await handle_photo_message(task, llm, api, msg_repo, payment_repo)
    if getattr(task, 'is_image_generation', False):
        return await handle_image_generation(task, api, session, msg_repo, payment_repo, gen_repo, llm)

    # --- ТЕКСТОВАЯ ГЕНЕРАЦИЯ ---
    ERROR_MESSAGES = ["Ой, я немного задумалась... Попробуй написать ещё раз? 😊",
                      "Хм, что-то меня отвлекло. Повтори, пожалуйста?", "Прости, я на секунду потеряла мысль."]
    answer, is_real_answer, is_fallback = "", False, False
    base_messages = []

    try:
        await maybe_generate_summary(session=session, llm=llm, msg_repo=msg_repo, summary_repo=summary_repo,
                                     user_id=task.user_id, character_id=task.char_id,
                                     model_override=getattr(task, 'model_name', None))
        await asyncio.sleep(0.5)

        base_messages = await build_llm_context(msg_repo=msg_repo, summary_repo=summary_repo, user_id=task.user_id,
                                                character_id=task.char_id,
                                                system_prompt=task.char_dict["system_prompt"])
        historical_assistant_msgs = [msg["content"] for msg in base_messages if msg.get("role") == "assistant"]

        base_settings = getattr(llm, '_settings', None)
        base_temperature = getattr(base_settings, 'temperature', 0.8) if base_settings else 0.8

        for attempt in range(MAX_REGEN_ATTEMPTS + 1):
            messages_to_send = base_messages.copy()  # copy.deepcopy не нужен для простого списка dict, если не меняем вложенные mutable объекты, но оставим как было для безопасности
            import copy
            messages_to_send = copy.deepcopy(base_messages)

            if attempt > 0 and base_settings is not None:
                llm._settings = copy.deepcopy(base_settings)
                llm._settings.temperature = min(base_temperature + 0.3, 1.2)
                messages_to_send.append({"role": "system",
                                         "content": "[OOC: Предыдущий ответ отклонен. Сохраняя характер, опиши реакцию через новую деталь, избегая повторов.]"})

            try:
                result = await llm.generate(messages_to_send, model_override=getattr(task, 'model_name', None))
            finally:
                if attempt > 0 and base_settings is not None:
                    llm._settings = base_settings

            if not result.success: break

            candidate_answer = truncate(clean_response(result.content))
            if not candidate_answer or not candidate_answer.strip():
                if attempt < MAX_REGEN_ATTEMPTS: continue
                answer, is_real_answer, is_fallback = f"*{task.char_dict.get('name', 'Персонаж')} задумчиво молчит.*", True, True
                break

            is_drunk, _ = is_drunk_response(candidate_answer)
            if is_drunk:
                if attempt < MAX_REGEN_ATTEMPTS: continue
                answer, is_real_answer, is_fallback = f"*{task.char_dict.get('name', 'Персонаж')} смущенно моргает, потеряв нить разговора, и мягко переводит тему.*", True, True
                break

            if is_ai_refusal(candidate_answer):
                answer, is_real_answer, is_fallback = f"*{task.char_dict.get('name', 'Персонаж')} делает паузу и мягко меняет тему, улыбнувшись*", True, True
                break

            if is_support:
                answer, is_real_answer = candidate_answer, True
                break

            is_dup, _ = is_duplicate_response(candidate_answer, historical_assistant_msgs, task.text)
            if is_dup:
                if attempt < MAX_REGEN_ATTEMPTS: continue
                answer, is_real_answer, is_fallback = f"*{task.char_dict.get('name', 'Персонаж')} мягко переводит тему, заглядывая тебе в глаза с новой эмоцией.*", True, True
                break

            answer, is_real_answer = candidate_answer, True
            break

        if is_real_answer and not is_fallback:
            await msg_repo.add_message(task.user_id, task.char_id, "assistant", answer)

    except Exception:
        logger.exception("💥 CRITICAL error in chat worker")

    # --- Фоллбэк и отправка ---
    try:
        is_debug_force_polza = get_settings().is_admin(
            task.user_dict['vk_user_id']) and "тест полза" in task.text.lower()

        if not is_support and (not is_real_answer or is_debug_force_polza):
            polza_answer = await rescue_with_polza(session, base_messages, get_llm_settings())
            if polza_answer:
                answer, is_real_answer = polza_answer, True
            else:
                answer = answer or random.choice(ERROR_MESSAGES)
        elif is_support and not is_real_answer:
            answer = answer or "Ой, что-то я задумалась... Попробуй переформулировать вопрос!"

        if not answer or not answer.strip():
            answer = f"*{task.char_dict.get('name', 'Персонаж')} на мгновение замерла, словно обдумывая твои слова, и мягко перевела тему.*"

        for send_attempt in range(2):
            try:
                await api.send_message(peer_id=int(task.peer_id), text=answer, keyboard=getattr(task, 'keyboard', None),
                                       attachment=getattr(task, 'attachment', None))
                break
            except Exception as vk_err:
                logger.error(f"🚨 VK Send attempt {send_attempt + 1} failed: {vk_err}")
                if send_attempt == 1:
                    logger.critical(f"❌ FAILED TO SEND TO USER {task.user_id}")
                else:
                    await asyncio.sleep(2)

        if not is_start_message and is_real_answer and not is_regeneration and not is_support and not get_settings().is_admin(
                task.user_dict['vk_user_id']):
            if await payment_repo.use_message(task.user_id):
                logger.info("💰 Energy used. New balance: %d", await payment_repo.get_user_balance(task.user_id))

    except Exception:
        logger.exception("💥 CRITICAL: Полный крах при отправке сообщения")

    logger.info("🏁 FINISHED task for user=%s", task.user_id)