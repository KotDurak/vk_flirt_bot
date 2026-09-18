#app/handlers/messages.py
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
from app.db.repositories.payments import PaymentRepository
from app.services.payments.base import PaymentProvider
from app.services.memory import maybe_generate_summary, build_llm_context
from app.config import get_llm_settings
from app.services.chat_queue import ChatTask
from app.config import get_settings, get_llm_settings

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

    text = re.sub(r'\([^)]*Примечание[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*Note[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()

    return text


async def shorten_url(long_url: str) -> str:
    """
    Сокращает ссылку через clck.ru (Яндекс).
    ВК гораздо лояльнее относится к коротким ссылкам и реже показывает предупреждения.
    """
    if not long_url:
        return ""
    try:
        async with aiohttp.ClientSession() as session:
            # clck.ru принимает исходный URL как параметр
            async with session.get(f"https://clck.ru/--?url={long_url}") as resp:
                if resp.status == 200:
                    short_url = await resp.text()
                    logger.info("🔗 URL shortened: %s -> %s", long_url, short_url.strip())
                    return short_url.strip()
    except Exception as e:
        logger.warning("⚠️ Failed to shorten URL: %s. Using original.", e)

    # Если сокращение не удалось, возвращаем оригинал
    return long_url

def get_main_menu_keyboard() -> str:
    """Главное меню бота."""
    kb = KeyboardBuilder(one_time=False)
    kb.add_button("👤 Выбрать персонажа", payload={"cmd": "chars"}, color="primary")
    kb.row()
    kb.add_button("⚡ Купить энергию", payload={"cmd": "buy"}, color="positive")
    kb.row()
    kb.add_button("📊 Мой профиль", payload={"cmd": "profile"}, color="secondary")
    kb.add_button("ℹ️ Помощь", payload={"cmd": "help"}, color="secondary")
    kb.row()
    kb.add_button("🔄 Сбросить диалог", payload={"cmd": "reset"}, color="negative")
    return kb.to_json()

def get_dialog_keyboard() -> str:
    """Клавиатура во время активного диалога (нижняя панель)."""
    kb = KeyboardBuilder(one_time=False)
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")
    return kb.to_json()

def get_regenerate_inline_keyboard() -> str:
    """Только кнопка регенерации, прикрепленная к сообщению."""
    kb = KeyboardBuilder(one_time=False, inline=True)
    kb.add_button("🔄 Перегенерировать (-1 ⚡)", payload={"cmd": "regenerate"}, color="secondary")
    return kb.to_json()


def get_characters_keyboard(characters: list[dict], per_row: int = 2) -> str:
    """Создает клавиатуру со списком персонажей."""
    kb = KeyboardBuilder(one_time=False)

    for i, char in enumerate(characters):
        kb.add_button(
            label=char["name"],
            payload={"cmd": "select_char", "char_id": char["id"]},
            color="primary"
        )
        # Переходим на новый ряд после каждых per_row кнопок
        if (i + 1) % per_row == 0:
            kb.row()

    # Перед кнопкой "В главное меню" всегда делаем новый ряд
    kb.row()
    kb.add_button("⬅️ В главное меню", payload={"cmd": "start"}, color="secondary")
    return kb.to_json()


def get_characters_paginated_keyboard(characters: list[dict], page: int, total_pages: int, per_row: int = 2) -> str:
    """Создает инлайн-клавиатуру со списком персонажей с пагинацией."""
    # Используем inline=True, чтобы клавиатура не перекрывала поле ввода и выглядела аккуратно
    kb = KeyboardBuilder(one_time=False, inline=True)

    for i, char in enumerate(characters):
        # Обрезаем имя, чтобы оно гарантированно влезло в кнопку VK (макс ~40 символов, лучше меньше)
        label = char["name"][:22] + "..." if len(char["name"]) > 22 else char["name"]
        kb.add_button(
            label=label,
            payload={"cmd": "select_char", "char_id": char["id"]},
            color="primary"
        )
        if (i + 1) % per_row == 0:
            kb.row()

    # Ряд навигации по страницам
    kb.row()
    if page > 1:
        kb.add_button("⬅️ Назад", payload={"cmd": "chars", "page": page - 1}, color="secondary")

    if page < total_pages:
        kb.add_button("Вперед ➡️", payload={"cmd": "chars", "page": page + 1}, color="secondary")

    # Ряд с выходом в главное меню
    kb.row()
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")

    return kb.to_json()


def get_character_actions_keyboard(char_id: int) -> str:
    """Клавиатура после выбора персонажа."""
    kb = KeyboardBuilder(one_time=False)
    kb.add_button("💬 Начать общение", payload={"cmd": "chat"}, color="positive")
    kb.row()
    kb.add_button("👥 Другие персонажи", payload={"cmd": "chars"}, color="primary")
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")
    return kb.to_json()


def get_payment_keyboard() -> str:
    """Меню покупки энергии."""
    kb = KeyboardBuilder(one_time=False, inline=True)
    kb.add_button("50 энергии (50₽)", payload={"cmd": "buy_package", "energy": 50, "amount": 50}, color="primary")
    kb.row()
    kb.add_button("200 энергии (150₽)", payload={"cmd": "buy_package", "energy": 200, "amount": 150}, color="primary")
    kb.row()
    kb.add_button("500 энергии (300₽)", payload={"cmd": "buy_package", "energy": 500, "amount": 300}, color="primary")
    kb.row()
    kb.add_button("⬅️ В главное меню", payload={"cmd": "start"}, color="secondary")
    return kb.to_json()


def get_payment_action_keyboard(payment_url: str, invoice_id: str) -> str:
    """Клавиатура с изящной кнопкой-ссылкой на оплату."""
    keyboard = {
        "one_time": False,
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "open_link",
                        "link": payment_url,  # Сюда передаем short_url или обычную ссылку
                        "label": "💳 Оплатить"
                    }
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "payload": json.dumps({"cmd": "check_payment", "invoice_id": invoice_id}),
                        "label": "✅ Я оплатил, проверить"
                    },
                    "color": "positive"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "payload": json.dumps({"cmd": "start"}),
                        "label": "⬅️ В главное меню"
                    },
                    "color": "secondary"
                }
            ]
        ]
    }
    return json.dumps(keyboard)


def get_check_payment_keyboard(invoice_id: str) -> str:
    """Кнопка для проверки статуса платежа."""
    kb = KeyboardBuilder(one_time=False, inline=True)
    kb.add_button(
        "✅ Я оплатил, проверить",
        payload={"cmd": "check_payment", "invoice_id": invoice_id},
        color="positive"
    )
    kb.row()
    kb.add_button("⬅️ В главное меню", payload={"cmd": "start"}, color="secondary")
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
        payment_repo: PaymentRepository,
        payment_provider: PaymentProvider,
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
    attachment = None
    settings = get_llm_settings()

    # === ГЛАВНОЕ МЕНЮ ===
    if text_lower == "начать" or cmd == "start":
        balance = await payment_repo.get_user_balance(user["id"])
        answer = (
            f"Привет! 👋 Я твой виртуальный компаньон для общения.\n\n"
            f"⚡ У тебя осталось {balance} энергии\n\n"
            f"Выбери, что хочешь сделать, с помощью кнопок ниже!"
        )
        send_keyboard = get_main_menu_keyboard()

    # === СПИСОК ПЕРСОНАЖЕЙ ===
    # === СПИСОК ПЕРСОНАЖЕЙ (С НАСТОЯЩЕЙ БД-ПАГИНАЦИЕЙ) ===
    elif cmd == "chars" or text_lower in ("персонажи", "выбрать персонажа", "персонаж"):
        page = int(payload.get("page", 1))
        chars_per_page = 6  # 2 в ряд * 3 ряда = 6 кнопок (идеально для inline)

        # 1. Узнаем общее количество, чтобы рассчитать страницы
        total_chars = await char_repo.get_active_characters_count()

        if total_chars == 0:
            answer = "Пока нет доступных персонажей. Загляни позже! 😿"
            send_keyboard = get_main_menu_keyboard()
        else:
            total_pages = (total_chars + chars_per_page - 1) // chars_per_page

            # Защита от некорректных номеров страниц (например, если кто-то подделал payload)
            if page < 1:
                page = 1
            elif page > total_pages:
                page = total_pages

            # 2. Загружаем ТОЛЬКО нужных персонажей из БД (быстро и экономно)
            offset = (page - 1) * chars_per_page
            page_chars = await char_repo.get_active_characters_paginated(chars_per_page, offset)

            # 3. Формируем красивое текстовое описание
            answer = f"👥 Доступные персонажи (Страница {page} из {total_pages}):\n\n"
            for i, char in enumerate(page_chars, start=offset + 1):
                desc = char.get("description", "Описание отсутствует")
                # Обрезаем описание, чтобы сообщение не было гигантским, но давало понять суть
                short_desc = desc[:120] + "…" if len(desc) > 120 else desc
                answer += f"{i}. **{char['name']}**\n{short_desc}\n\n"

            answer += "Нажми на имя персонажа ниже, чтобы выбрать его 👇"

            # 4. Генерируем клавиатуру (функцию get_characters_paginated_keyboard мы добавили в прошлом шаге)
            send_keyboard = get_characters_paginated_keyboard(page_chars, page, total_pages)

    # === ВЫБОР КОНКРЕТНОГО ПЕРСОНАЖА ===
    elif cmd == "select_char":
        char_id = payload.get("char_id")
        character = await char_repo.get_by_id(char_id)

        if not character:
            answer = "Персонаж не найден 😿"
            send_keyboard = get_main_menu_keyboard()
        else:
            # 1. Сначала узнаем, был ли у пользователя ДРУГОЙ персонаж до этого
            old_char = await char_repo.get_user_character(user["id"])

            # 2. Если был, и он НЕ совпадает с новым, стираем его историю и саммари
            if old_char and old_char["id"] != character["id"]:
                await msg_repo.clear_history(user["id"], old_char["id"])
                await summary_repo.clear_summary(user["id"], old_char["id"])
                logger.info("🧹 Cleared history for old character %s", old_char["id"])

            # 3. Устанавливаем нового персонажа
            await char_repo.set_user_character(user["id"], character["id"])

            # 4. На всякий случай чистим историю нового персонажа (она и так должна быть пустой)
            await msg_repo.clear_history(user["id"], character["id"])
            await summary_repo.clear_summary(user["id"], character["id"])

            answer = (
                f"✨ {character['name']}\n\n"
                f"{character['description']}\n\n"
                f"Выбор сохранен! Что будем делать?"
            )
            send_keyboard = get_character_actions_keyboard(character["id"])

            if character.get("photo_attachment"):
                attachment = character["photo_attachment"]

    # === ПРОФИЛЬ И СТАТИСТИКА ===
    elif cmd == "profile":
        balance = await payment_repo.get_user_balance(user["id"])
        stats = await payment_repo.get_user_stats(user["id"])
        current_char = await char_repo.get_user_character(user["id"])

        char_name = current_char["name"] if current_char else "не выбран"

        answer = (
            f"📊 Твой профиль\n\n"
            f"⚡ Энергия: {balance}\n"
            f"💬 Всего сообщений: {stats['total_messages']}\n"
            f"💰 Всего куплено энергии: {stats['total_energy_bought']}\n"
            f"🎭 Текущий персонаж: {char_name}\n"
            f"📅 Ты с нами с: {user.get('created_at', 'неизвестно')}\n\n"
            f"Продолжай общение, чтобы узнать больше! 😉"
        )
        send_keyboard = get_main_menu_keyboard()

    # === СПРАВКА ===
    elif text_lower in ("/help", "помощь") or cmd == "help":
        answer = (
            "ℹ️ Справка:\n\n"
            "Просто пиши мне сообщения, и мы будем болтать!\n"
            "Каждое сообщение тратит 1 энергию ⚡\n"
            "Ты можешь выбрать персонажа, сбросить нашу историю или вызвать это меню.\n\n"
            "Команды:\n"
            "/start - Главное меню\n"
            "/help - Справка\n"
            "/reset - Сбросить диалог"
        )
        send_keyboard = get_main_menu_keyboard()

    # === СБРОС ===
    elif text_lower in ("/reset", "сброс", "сбросить") or cmd == "reset":
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

    # === ПОКУПКА ЭНЕРГИИ ===
    elif cmd == "buy":
        balance = await payment_repo.get_user_balance(user["id"])
        answer = (
            f"⚡ Магазин энергии\n\n"
            f"Текущий баланс: {balance} энергии\n\n"
            f"Выбери пакет:"
        )
        send_keyboard = get_payment_keyboard()

    # === СОЗДАНИЕ ИНВОЙСА ===
    elif cmd == "buy_package":
        energy = payload.get("energy")
        amount = payload.get("amount")

        result = await payment_provider.create_invoice(
            user_id=user["id"],
            amount=amount,
            messages=energy,
        )

        if result.success:
            await payment_repo.create_payment(
                user_id=user["id"],
                invoice_id=result.invoice_id,
                amount=amount,
                messages=energy,
            )

            # Сокращаем ссылку для красоты и надежности (VK лучше относится к коротким URL)
            short_url = await shorten_url(result.payment_url)

            answer = (
                f"💳 Оформление заказа\n\n"
                f"Пакет: {energy} энергии\n"
                f"К оплате: {amount}₽\n\n"
                f"Нажмите на кнопку ниже, чтобы перейти к безопасной оплате. "
                f"После успешного перевода не забудьте нажать «Проверить»!"
            )

            # Используем новую красивую клавиатуру вместо старой
            send_keyboard = get_payment_action_keyboard(short_url, result.invoice_id)

        else:
            answer = f"❌ Ошибка создания платежа: {result.error_message}"
            send_keyboard = get_payment_keyboard()

    # === ПРОВЕРКА СТАТУСА ПЛАТЕЖА ===
    elif cmd == "check_payment":
        invoice_id = payload.get("invoice_id")

        payment = await payment_repo.get_payment_by_invoice(invoice_id)

        if not payment:
            answer = "❌ Платёж не найден"
            send_keyboard = get_main_menu_keyboard()
        elif payment["status"] == "paid":
            answer = "✅ Этот платёж уже обработан!"
            send_keyboard = get_main_menu_keyboard()
        else:
            status = await payment_provider.check_status(invoice_id)

            if status.is_paid:
                await payment_repo.mark_as_paid(invoice_id)
                await payment_repo.add_user_messages(
                    user_id=user["id"],
                    messages=payment["messages"]
                )

                new_balance = await payment_repo.get_user_balance(user["id"])

                answer = (
                    f"🎉 Оплата получена!\n\n"
                    f"⚡ Начислено {payment['messages']} энергии\n"
                    f"💬 Текущий баланс: {new_balance} энергии\n\n"
                    f"Можешь продолжать общение!"
                )
                send_keyboard = get_main_menu_keyboard()
            else:
                answer = (
                    f"⏳ Платёж ещё не обработан\n\n"
                    f"Если ты уже оплатил, подожди 1-2 минуты и нажми 'Проверить' снова.\n\n"
                    f"Статус: {status.status}"
                )
                send_keyboard = get_check_payment_keyboard(invoice_id)

    # === СМЕНА МОДЕЛИ (ТОЛЬКО ДЛЯ АДМИНА) ===
    elif text_lower.startswith("/model") or cmd == "model":
        if not get_settings().is_admin(int(from_id)):
            answer = "🔒 Эта команда доступна только администратору."
            send_keyboard = get_main_menu_keyboard()
        else:
            # Импортируем здесь, чтобы избежать циклических зависимостей, если MODELS_LIST в config
            from app.config import MODELS_LIST
            available_models = list(MODELS_LIST.keys())
            parts = text_lower.split(maxsplit=1)

            if len(parts) == 1:
                # Показать текущую и список
                current_model = user.get("preferred_model") or get_llm_settings().model
                msg = f"⚙️ Твоя текущая модель: `{current_model}`\n\n📋 Доступные модели:\n"
                for i, m_key in enumerate(available_models, 1):
                    msg += f"{i}. `{m_key}`\n"
                msg += "\n💡 Чтобы сменить, напиши: `/model <номер>` или `/model <точное_название>`"
                answer = msg
                send_keyboard = get_main_menu_keyboard()
            else:
                query = parts[1].strip()
                new_model = None

                # Если ввели номер
                if query.isdigit():
                    idx = int(query) - 1
                    if 0 <= idx < len(available_models):
                        new_model = available_models[idx]
                # Если ввели название
                elif query in available_models:
                    new_model = query

                if new_model:
                    await user_repo.update_preferred_model(int(from_id), new_model)
                    answer = f"✅ Модель успешно изменена на: `{new_model}`"
                else:
                    answer = "❌ Модель не найдена. Используй `/model` для просмотра списка."
                send_keyboard = get_main_menu_keyboard()
    # === ПЕРЕГЕНЕРАЦИЯ ОТВЕТА ===
    elif cmd == "regenerate":
        current_char = await char_repo.get_user_character(user["id"])

        if not current_char:
            answer = "Сначала выбери персонажа! 👇"
            send_keyboard = get_main_menu_keyboard()
        else:
            balance = await payment_repo.get_user_balance(user["id"])
            target_model = settings.model
            if balance <= 0:
                answer = "😿 У тебя закончилась энергия!\n\nКупи новый пакет, чтобы продолжить:"
                send_keyboard = get_payment_keyboard()
            else:
                history = await msg_repo.get_recent_history(user["id"], current_char["id"], limit=2)

                if len(history) < 2 or history[-1]["role"] != "assistant":
                    answer = "Нечего перегенерировать. Напиши что-нибудь первым! 😉"
                    send_keyboard = get_dialog_keyboard()
                else:
                    success = await payment_repo.deduct_messages(user["id"], 1)
                    if not success:
                        answer = "😿 Не удалось списать энергию. Попробуй позже."
                        send_keyboard = get_main_menu_keyboard()
                    else:
                        # 1. Удаляем неудачный ответ ассистента из БД
                        await msg_repo.delete_last_assistant_message(user["id"], current_char["id"])

                        # 2. Берем текст последнего сообщения ПОЛЬЗОВАТЕЛЯ
                        last_user_text = history[-2]["content"]

                        # 3. Индикатор загрузки (БЕЗ keyboard, чтобы не стирать нижнее меню!)
                        await api.send_message(
                            peer_id=int(peer_id),
                            text="🔄 Перегенерация ответа..."
                        )
                        active_model = user.get("preferred_model") or target_model

                        # 4. Отправляем задачу в очередь заново с inline-клавиатурой
                        await chat_queue.add(ChatTask(
                            user_id=user["id"],
                            char_id=current_char["id"],
                            peer_id=int(peer_id),
                            text=last_user_text,
                            user_dict=user,
                            char_dict=current_char,
                            keyboard=get_regenerate_inline_keyboard(),
                            is_regeneration=True,
                            model_name=active_model,
                        ))
                        return

    # === ОБЫЧНЫЙ ДИАЛОГ С ПЕРСОНАЖЕМ ===
    else:
        if cmd == "chat":
            current_char = await char_repo.get_user_character(user["id"])

            if not current_char:
                answer = "Сначала выбери персонажа! 👇"
                send_keyboard = get_main_menu_keyboard()
            else:
                target_model = settings.model
                existing_messages = await msg_repo.get_recent_history(user["id"], current_char["id"], limit=1)
                if existing_messages:
                    logger.info("⏭️ Dialog already started for user %s, ignoring", user["id"])
                    return

                greeting = current_char.get("greeting_message")
                if greeting:
                    await msg_repo.add_message(user["id"], current_char["id"], "assistant", greeting)
                    await api.send_message(
                        peer_id=int(peer_id),
                        text=greeting,
                        keyboard=get_dialog_keyboard(),
                    )
                    logger.info("✅ Sent predefined greeting for user %s", user["id"])
                    return

                balance = await payment_repo.get_user_balance(user["id"])
                if balance <= 0:
                    answer = "😿 У тебя закончилась энергия!\n\nКупи новый пакет, чтобы продолжить общение:"
                    send_keyboard = get_payment_keyboard()
                    await api.send_message(peer_id=int(peer_id), text=answer, keyboard=send_keyboard)
                    return

                # Индикатор загрузки (БЕЗ keyboard)
                await api.send_message(peer_id=int(peer_id), text="⏳")
                active_model = user.get("preferred_model") or target_model

                await chat_queue.add(ChatTask(
                    user_id=user["id"],
                    char_id=current_char["id"],
                    peer_id=int(peer_id),
                    text="[СИСТЕМНАЯ КОМАНДА: Сгенерируй САМОЕ ПЕРВОЕ сообщение диалога. Обстановка: простая и повседневная. Ты занята своими делами. Реакция: холодная, ленивая, с легким скепсисом. Формат: 1-2 коротких абзаца, действия в *звездочках*, речь с тире.]",
                    user_dict=user,
                    char_dict=current_char,
                    keyboard=get_regenerate_inline_keyboard(),
                    model_name=active_model
                ))
                return

        if payload.get("cmd") or text.startswith("💬") or text.startswith("👤"):
            logger.info("⏭️ Skipping service/payload message: %s", text[:50])
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
                target_model = settings.model
                balance = await payment_repo.get_user_balance(user["id"])
                if balance <= 0:
                    answer = "😿 У тебя закончилась энергия!\n\nКупи новый пакет, чтобы продолжить общение:"
                    send_keyboard = get_payment_keyboard()
                    await api.send_message(peer_id=int(peer_id), text=answer, keyboard=send_keyboard)
                    return

                char_id = current_char["id"]
                await msg_repo.add_message(user["id"], char_id, "user", text)
                active_model = user.get("preferred_model") or target_model
                await chat_queue.add(ChatTask(
                    user_id=user["id"],
                    char_id=char_id,
                    peer_id=int(peer_id),
                    text=text,
                    user_dict=user,
                    char_dict=current_char,
                    keyboard=get_regenerate_inline_keyboard(),
                    model_name=active_model
                ))
                return

    # ЕДИНСТВЕННАЯ отправка системного ответа в конце (дубликат удален)
    await api.send_message(
        peer_id=int(peer_id),
        text=answer,
        keyboard=send_keyboard,
        attachment=attachment,
    )