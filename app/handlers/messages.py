# app/handlers/messages.py
from __future__ import annotations

import json
import logging
from typing import Any
import asyncio
import aiohttp
from app.services.event_cache import EventCache
from app.vk.api import VKApi
from app.vk.keyboard import KeyboardBuilder
from app.services.llm import LLMBase
from app.db.repositories.users import UserRepository
from app.db.repositories.characters import CharacterRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.summaries import SummaryRepository
from app.services.memory import maybe_generate_summary, build_llm_context
from app.services.chat_queue import ChatTask

logger = logging.getLogger(__name__)


def _extract_message(update_object: dict[str, Any]) -> dict[str, Any]:
    message = update_object.get("message")
    if isinstance(message, dict):
        return message
    return update_object


def _truncate(value: str, limit: int = 1500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _clean_response(text: str) -> str:
    """Убирает английские вставки и мета-текст из ответа LLM."""
    import re

    # Словарь замен английских слов на русские
    replacements = {
        r'\bhandsome\b': 'красавчик',
        r'\bbaby\b': 'малыш',
        r'\bsweetheart\b': 'милый',
        r'\bhoney\b': 'солнце',
        r'\bdarling\b': 'дорогой',
        r'\bcute\b': 'милый',
        r'\bhey\b': 'привет',
        r'\bhi\b': 'привет',
        r'\bhello\b': 'привет',
        r'\bokay\b': 'хорошо',
        r'\bwow\b': 'вау',
        r'\bsorry\b': 'прости',
        r'\byeah\b': 'да',
    }

    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # Убираем примечания в скобках (модель иногда пишет мета-текст)
    text = re.sub(r'\([^)]*Примечание[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*Note[^)]*\)', '', text, flags=re.IGNORECASE)

    # Убираем лишние пробелы
    text = re.sub(r'\s+', ' ', text).strip()

    return text

def get_main_menu_keyboard() -> str:
    """Главное меню бота."""
    kb = KeyboardBuilder(one_time=False)
    kb.add_button("👤 Выбрать персонажа", payload={"cmd": "chars"}, color="primary")
    kb.row()
    kb.add_button("ℹ️ Помощь", payload={"cmd": "help"}, color="secondary")
    kb.add_button("🔄 Сбросить диалог", payload={"cmd": "reset"}, color="negative")
    return kb.to_json()


def get_characters_keyboard(characters: list[dict]) -> str:
    """Создает клавиатуру со списком персонажей."""
    kb = KeyboardBuilder(one_time=False)
    for char in characters:
        kb.add_button(
            label=char["name"],
            payload={"cmd": "select_char", "char_id": char["id"]},
            color="primary"
        )
        kb.row()

    # Кнопка назад в главное меню
    kb.add_button("⬅️ В главное меню", payload={"cmd": "start"}, color="secondary")
    return kb.to_json()


def get_character_actions_keyboard(char_id: int) -> str:
    """Клавиатура после выбора персонажа."""
    kb = KeyboardBuilder(one_time=False)
    kb.add_button("💬 Начать общение", payload={"cmd": "chat"}, color="positive")
    kb.row()
    kb.add_button("👥 Другие персонажи", payload={"cmd": "chars"}, color="primary")
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")
    return kb.to_json()


async def handle_update(
        update: dict[str, Any],
        api: VKApi,
        group_id: int,
        session: aiohttp.ClientSession,
        user_repo: UserRepository,
        char_repo: CharacterRepository,
        msg_repo: MessageRepository,
        summary_repo: SummaryRepository,
        event_cache: EventCache,
        chat_queue,
) -> None:
    update_type = update.get("type")
    event_id = update.get("event_id")
    if event_id and event_cache.is_duplicate(event_id):
        logger.debug("Duplicate event ignored: %s", event_id)
        return

    if update_type != "message_new":
        return

    update_object = update.get("object")
    if not isinstance(update_object, dict):
        return

    message = _extract_message(update_object)
    if not isinstance(message, dict):
        return

    if message.get("action"):
        return

    from_id = message.get("from_id") or message.get("user_id")
    peer_id = message.get("peer_id") or from_id
    if not peer_id:
        return

    if isinstance(from_id, int) and from_id == -abs(group_id):
        return

    user = await user_repo.get_or_create(int(from_id))

    text = (message.get("text") or "").strip()
    payload_str = message.get("payload")

    logger.info("👉 Входящее: user_id=%s, text='%s', payload='%s'", from_id, text, payload_str)

    payload = {}
    if payload_str:
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            pass

    cmd = payload.get("cmd") or payload.get("command") or ""
    text_lower = text.lower()

    answer = ""
    send_keyboard = None
    attachment = None  # <-- Для фото персонажа

    # === ГЛАВНОЕ МЕНЮ ===
    if text_lower == "начать" or cmd == "start":
        answer = (
            "Привет! 👋 Я твой виртуальный компаньон для общения.\n"
            "Выбери, что хочешь сделать, с помощью кнопок ниже!"
        )
        send_keyboard = get_main_menu_keyboard()

    # === СПИСОК ПЕРСОНАЖЕЙ ===
    elif cmd == "chars":
        characters = await char_repo.get_all_active()
        if not characters:
            answer = "Пока нет доступных персонажей. Загляни позже! 😿"
            send_keyboard = get_main_menu_keyboard()
        else:
            answer = "👥 Выбери, с кем хочешь пообщаться:"
            send_keyboard = get_characters_keyboard(characters)

    # === ВЫБОР КОНКРЕТНОГО ПЕРСОНАЖА ===
    elif cmd == "select_char":
        char_id = payload.get("char_id")
        character = await char_repo.get_by_id(char_id)

        if not character:
            answer = "Персонаж не найден 😿"
            send_keyboard = get_main_menu_keyboard()
        else:
            # Сохраняем выбор пользователя
            await char_repo.set_user_character(user["id"], character["id"])

            # === НОВОЕ: Очищаем историю и summary для нового персонажа ===
            await msg_repo.clear_history(user["id"], character["id"])
            await summary_repo.clear_summary(user["id"], character["id"])
            # =============================================================

            answer = (
                f"✨ {character['name']}\n\n"
                f"{character['description']}\n\n"
                f"Выбор сохранен! Что будем делать?"
            )
            send_keyboard = get_character_actions_keyboard(character["id"])

            if character.get("photo_attachment"):
                attachment = character["photo_attachment"]

    # === СПРАВКА ===
    elif text_lower in ("/help", "помощь") or cmd == "help":
        answer = (
            "ℹ️ Справка:\n\n"
            "Просто пиши мне сообщения, и мы будем болтать!\n"
            "Ты можешь выбрать персонажа, сбросить нашу историю или вызвать это меню.\n\n"
            "Команды:\n"
            "/start - Главное меню\n"
            "/help - Справка\n"
            "/reset - Сбросить диалог"
        )
        send_keyboard = get_main_menu_keyboard()

    # === СБРОС ===
    elif text_lower in ("/reset", "сброс", "сбросить") or cmd == "reset":
        # Очищаем историю и summary для текущего персонажа
        current_char = await char_repo.get_user_character(user["id"])
        if current_char:
            await msg_repo.clear_history(user["id"], current_char["id"])
            await summary_repo.clear_summary(user["id"], current_char["id"])
            answer = f"🔄 Наша история с {current_char['name']} сброшена! Начнем всё с чистого листа? 😉"
        else:
            await msg_repo.clear_history(user["id"])
            await summary_repo.clear_summary(user["id"])
            answer = "🔄 Вся история сброшена! Начнем всё с чистого листа? 😉"
        send_keyboard = get_main_menu_keyboard()

    # === ОБЫЧНЫЙ ДИАЛОГ С ПЕРСОНАЖЕМ ===
    else:
        if cmd == "chat":
            current_char = await char_repo.get_user_character(user["id"])
            if not current_char:
                answer = "Сначала выбери персонажа! 👇"
                send_keyboard = get_main_menu_keyboard()
            else:
                # Кидаем в очередь стартовую задачу — персонаж сам поздоровается
                await chat_queue.add(ChatTask(
                    user_id=user["id"],
                    char_id=current_char["id"],
                    peer_id=int(peer_id),
                    text="[Начало диалога. Поздоровайся с пользователем и задай тон сцене.]",
                    user_dict=user,
                    char_dict=current_char,
                ))
                # Ответ отправит воркер, тут выходим
                return
        # 🆕 Фильтруем служебные сообщения (кнопки)
        if payload.get("cmd") or text.startswith("💬") or text.startswith("👤"):
            logger.info("⏭️ Skipping service/payload message: %s", text[:50])
            # Можно отправить меню или игнорировать
            return
        if not text:
            answer = "Напиши мне что-нибудь, я умею не только молчать 😉"
            send_keyboard = get_main_menu_keyboard()
        else:
            current_char = await char_repo.get_user_character(user["id"])

            if not current_char:
                answer = "Сначала выбери персонажа, с которым хочешь пообщаться! 👇"
                send_keyboard = get_main_menu_keyboard()
            else:
                char_id = current_char["id"]

                # 1. Сохраняем сообщение пользователя
                await msg_repo.add_message(user["id"], char_id, "user", text)

                # 2. Кидаем задачу в очередь
                await chat_queue.add(ChatTask(
                    user_id=user["id"],
                    char_id=char_id,
                    peer_id=int(peer_id),
                    text=text,
                    user_dict=user,
                    char_dict=current_char,
                ))

                # Ответ отправит воркер, тут выходим
                return

    await api.send_message(
        peer_id=int(peer_id),
        text=answer,
        keyboard=send_keyboard,
        attachment=attachment,
    )