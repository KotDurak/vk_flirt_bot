# app/handlers/messages.py
# ВАЖНО! В этом файле запрещено писать функции, логику!!! Кто напишет того Барсик покусает
from __future__ import annotations

import json
import logging
from typing import Any
import aiohttp
import time
from datetime import datetime, timedelta

from app.db.repositories.promo import PromoRepository
from app.services.event_cache import EventCache
from app.vk.api import VKApi
from app.db.repositories.users import UserRepository
from app.db.repositories.characters import CharacterRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.summaries import SummaryRepository
from app.db.repositories.payments import PaymentRepository
from app.services.payments.base import PaymentProvider
from app.services.chat_queue import ChatTask
from app.config import get_settings, get_llm_settings
from app.db.repositories.support import SupportRepository

from app.vk.keyboards import (
    get_main_menu_keyboard,
    get_characters_keyboard,
    get_characters_paginated_keyboard,
    get_character_actions_keyboard,
    get_payment_keyboard,
    get_payment_action_keyboard,
    get_check_payment_keyboard,
    get_regenerate_inline_keyboard,
    get_admin_promo_list_keyboard,
    get_dialog_keyboard,
    get_admin_ticket_list_keyboard,
    get_admin_ticket_actions_keyboard,
    get_referral_keyboard,
)
from app.utils import (
    _extract_message,
    _truncate,
    _clean_response,
    shorten_url,
    generate_and_upload_qr,
)

logger = logging.getLogger(__name__)

awaiting_support = {}


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
        promo_repo: PromoRepository,
        support_repo: SupportRepository,
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

    photo_url = None
    if message.get("attachments"):
        for att in message["attachments"]:
            if att["type"] == "photo":
                photo_url = att["photo"]["sizes"][-1]["url"]
                break

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

    # === ВЫБОР ПЕРСОНАЖА ПО SLUG (Команда /select) ===
    if text_lower == "/select" or text_lower.startswith("/select "):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            slug = parts[1].strip().lower()
            character = await char_repo.get_by_slug(slug)

            if not character:
                answer = f"😿 Персонаж '{slug}' не найден. Проверь правильность написания или выбери из меню."
                send_keyboard = get_main_menu_keyboard()
            else:
                old_char = await char_repo.get_user_character(user["id"])

                if old_char and old_char["id"] != character["id"]:
                    await msg_repo.clear_history(user["id"], old_char["id"])
                    await summary_repo.clear_summary(user["id"], old_char["id"])
                    logger.info("🧹 Cleared history for old character %s", old_char["id"])

                await char_repo.set_user_character(user["id"], character["id"])
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
        else:
            # Пользователь написал просто /select без аргумента
            answer = (
                "⚠️ **Как выбрать персонажа:**\n\n"
                "Самый удобный способ — использовать наш интерактивный каталог с поиском и фильтрами! Просто нажми на кнопку ниже 👇\n\n"
                "Или напиши вручную: `/select <имя>`\n"
                "Примеры:\n"
                "• `/select slani`\n"
                "• `/select khori`\n"
                "• `/select barsik`"
            )
            # Нативная кнопка-ссылка VK
            send_keyboard = {
                "inline": True,
                "buttons": [
                    [
                        {
                            "action": {
                                "type": "open_link",
                                "link": "https://vk.ru/app54816075",
                                "label": "🎭 Открыть каталог персонажей"
                            }
                        }
                    ]
                ]
            }

    # === ГЛАВНОЕ МЕНЮ ===
    elif text_lower == "начать" or cmd == "start":
        balance = await payment_repo.get_user_balance(user["id"])
        answer = (
            f"Привет! 👋 Я твой виртуальный компаньон для общения.\n\n"
            f"⚡ У тебя осталось {balance} энергии\n\n"
            f"Выбери, что хочешь сделать, с помощью кнопок ниже!\n\n"
            "🎨 Хочешь увидеть свою мечту в картинках? Твой личный художник по аниме-девочкам: @MyNekoBaka89_bot\n\n"
            "💻 А если нужна помощь с кодом или вопросами по IT — загляни к Шире-тян: @shira_neuro_bot"
        )
        send_keyboard = get_main_menu_keyboard()

    elif cmd == "referral":
        referral_code = f"vk{user['vk_user_id']}"
        answer = (
            f"🎁 Твой личный промокод: `{referral_code}`\n\n"
            "Поделись кодом! Когда друг введет его и напишет мне, он получит 10 энергии, а ты — 5 в качестве благодарности за рекомендацию."
        )
        send_keyboard = get_referral_keyboard(referral_code)

    # === СПИСОК ПЕРСОНАЖЕЙ ===
    elif cmd == "chars" or text_lower in ("персонажи", "выбрать персонажа", "персонаж"):
        page = int(payload.get("page", 1))
        chars_per_page = 6

        total_chars = await char_repo.get_active_characters_count()

        if total_chars == 0:
            answer = "Пока нет доступных персонажей. Загляни позже! 😿"
            send_keyboard = get_main_menu_keyboard()
        else:
            total_pages = (total_chars + chars_per_page - 1) // chars_per_page

            if page < 1:
                page = 1
            elif page > total_pages:
                page = total_pages

            offset = (page - 1) * chars_per_page
            page_chars = await char_repo.get_active_characters_paginated(chars_per_page, offset)

            answer = f"👥 Доступные персонажи (Страница {page} из {total_pages}):\n\n"
            for i, char in enumerate(page_chars, start=offset + 1):
                desc = char.get("description", "Описание отсутствует")
                short_desc = desc[:120] + "…" if len(desc) > 120 else desc
                answer += f"{i}. **{char['name']}**\n{short_desc}\n\n"

            answer += "Нажми на имя персонажа ниже, чтобы выбрать его 👇"
            send_keyboard = get_characters_paginated_keyboard(page_chars, page, total_pages)

    # === ВЫБОР КОНКРЕТНОГО ПЕРСОНАЖА (через кнопку) ===
    elif cmd == "select_char":
        char_id = payload.get("char_id")
        character = await char_repo.get_by_id(char_id)

        if not character:
            answer = "Персонаж не найден 😿"
            send_keyboard = get_main_menu_keyboard()
        else:
            old_char = await char_repo.get_user_character(user["id"])

            if old_char and old_char["id"] != character["id"]:
                await msg_repo.clear_history(user["id"], old_char["id"])
                await summary_repo.clear_summary(user["id"], old_char["id"])
                logger.info("🧹 Cleared history for old character %s", old_char["id"])

            await char_repo.set_user_character(user["id"], character["id"])
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
    elif text_lower == "/reset" or cmd == "reset":
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

    # === ПОДДЕРЖКА ===
    elif text_lower.startswith("/support") or cmd == "support":
        if text_lower in ("поддержка", "support", "🆘 поддержка", "🆘 support", "/support", "🆘"):
            support_text = ""
        else:
            parts = text.split(maxsplit=1)
            support_text = parts[1].strip() if len(parts) > 1 else ""

        attachments = message.get("attachments", [])

        if not support_text and not attachments:
            awaiting_support[int(from_id)] = time.time()
            answer = (
                "🆘 Служба поддержки\n\n"
                "Пожалуйста, опиши суть проблемы и прикрепи скриншот (если есть).\n\n"
                "💡 *Как отправить:*\n"
                "Просто напиши текст жалобы и прикрепи фото к этому сообщению. "
                "Если ты просто нажал кнопку — напиши суть проблемы следующим сообщением (можно просто отправить скриншот)."
            )
            send_keyboard = get_main_menu_keyboard()
        else:
            from app.services.support import process_support_request
            answer = await process_support_request(
                api=api,
                user_id=user["id"],
                vk_user_id=int(from_id),
                text=support_text,
                attachments=attachments,
                support_repo=support_repo
            )
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

            short_url = await shorten_url(result.payment_url)
            qr_attachment = await generate_and_upload_qr(api, short_url)

            answer = (
                f"💳 Оформление заказа\n\n"
                f"📦 Пакет: {energy} энергии\n"
                f"💰 К оплате: {amount}₽\n\n"
                f"📱 Отсканируй QR-код камерой телефона для быстрой оплаты,\n"
                f"или нажми на кнопку ниже 👇\n\n"
                f"После успешного перевода не забудь нажать «Проверить»!"
            )

            send_keyboard = get_payment_action_keyboard(short_url, result.invoice_id)
            final_attachment = qr_attachment if qr_attachment else short_url

            await api.send_message(
                peer_id=int(peer_id),
                text=answer,
                keyboard=send_keyboard,
                attachment=final_attachment
            )
            return
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
            from app.config import MODELS_LIST
            available_models = list(MODELS_LIST.keys())
            parts = text_lower.split(maxsplit=1)

            if len(parts) == 1:
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

                if query.isdigit():
                    idx = int(query) - 1
                    if 0 <= idx < len(available_models):
                        new_model = available_models[idx]
                elif query in available_models:
                    new_model = query

                if new_model:
                    await user_repo.update_preferred_model(int(from_id), new_model)
                    answer = f"✅ Модель успешно изменена на: `{new_model}`"
                else:
                    answer = "❌ Модель не найдена. Используй `/model` для просмотра списка."
                send_keyboard = get_main_menu_keyboard()

    # === АДМИН-ПАНЕЛЬ (ТОЛЬКО ДЛЯ АДМИНА) ===
    elif text_lower.startswith("/admin") or cmd.startswith("admin_"):
        if not get_settings().is_admin(int(from_id)):
            answer = "🔒 Эта команда доступна только администратору."
            send_keyboard = get_main_menu_keyboard()
        else:
            parts = text_lower.split()
            command = cmd if cmd else (parts[0] if parts else "")
            send_keyboard = get_main_menu_keyboard()

            if command in ("/admin_check", "/admin_info") and len(parts) >= 2:
                vk_id = int(parts[1])
                target_user = await user_repo.get_by_vk_id(vk_id)
                if not target_user:
                    answer = f"❌ Пользователь с VK ID {vk_id} не найден в БД."
                else:
                    user_id = target_user["id"]
                    balance = await payment_repo.get_user_balance(user_id)
                    stats = await payment_repo.get_user_stats(user_id)
                    current_char = await char_repo.get_user_character(user_id)
                    char_name = current_char["name"] if current_char else "не выбран"
                    answer = (
                        f"🔍 Инфо о пользователе {vk_id}:\n\n"
                        f"⚡ Баланс (messages): {balance}\n"
                        f"💬 Сообщений всего: {stats['total_messages']}\n"
                        f"💰 Куплено энергии: {stats['total_energy_bought']}\n"
                        f"🎭 Текущий персонаж: {char_name}"
                    )

            elif command in ("/admin_add", "/admin_add_energy") and len(parts) >= 3:
                vk_id = int(parts[1])
                amount = int(parts[2])
                target_user = await user_repo.get_by_vk_id(vk_id)
                if not target_user:
                    answer = f"❌ Пользователь с VK ID {vk_id} не найден."
                else:
                    user_id = target_user["id"]
                    await payment_repo.add_user_messages(user_id, amount)
                    new_balance = await payment_repo.get_user_balance(user_id)
                    answer = (
                        f"✅ Начислено {amount} энергии пользователю {vk_id}.\n"
                        f"Новый баланс: {new_balance} ⚡"
                    )

            elif command in ("/admin_reset", "/admin_reset_history") and len(parts) >= 2:
                vk_id = int(parts[1])
                target_user = await user_repo.get_by_vk_id(vk_id)
                if not target_user:
                    answer = f"❌ Пользователь с VK ID {vk_id} не найден."
                else:
                    user_id = target_user["id"]
                    current_char = await char_repo.get_user_character(user_id)
                    if current_char:
                        await msg_repo.clear_history(user_id, current_char["id"])
                        await summary_repo.clear_summary(user_id, current_char["id"])
                        answer = (
                            f"🔄 История пользователя {vk_id} успешно сброшена!\n"
                            f"(Персонаж: {current_char['name']})"
                        )
                    else:
                        await msg_repo.clear_history(user_id)
                        await summary_repo.clear_summary(user_id)
                        answer = f"🔄 Глобальная история пользователя {vk_id} сброшена."

            elif command == "/admin_refill_all" and len(parts) >= 2:
                target_amount = int(parts[1])
                affected_count = await user_repo.refill_all_messages(target_amount)
                answer = (
                    f"🚀 МАССОВОЕ ПОПОЛНЕНИЕ ВЫПОЛНЕНО!\n\n"
                    f"✅ {affected_count} пользователям установлен баланс {target_amount} ⚡\n"
                    f"(Затронуты только те, у кого messages < {target_amount})\n\n"
                    f"Можно публиковать пост в паблике! 🐾"
                )

            elif command == "/admin_promo_add" and len(parts) >= 3:
                code = parts[1].upper()
                try:
                    reward = int(parts[2])
                    if len(parts) > 3 and parts[3] != "0":
                        max_uses = int(parts[3])
                    else:
                        max_uses = None
                except ValueError:
                    answer = "❌ Ошибка: награда и лимит должны быть числами.\nПример: /admin_promo_add KITSUNE 50 100"
                else:
                    await promo_repo.create_promo_code(code, reward, max_uses)
                    limit_text = f"{max_uses} раз" if max_uses is not None else "Безлимитно"
                    answer = (
                        f"✅ Промокод успешно создан!\n\n"
                        f"Код: `{code}`\n"
                        f"Награда: {reward} энергии\n"
                        f"Общий лимит активаций: {limit_text}\n"
                        f"(Каждый пользователь может использовать его только 1 раз)"
                    )

            elif command == "/admin_promo_list" or cmd == "admin_promo_list":
                page = int(payload.get("page", 1))
                per_page = 10
                if page < 1:
                    page = 1

                promos, total = await promo_repo.get_all_active_promos(limit=per_page, offset=(page - 1) * per_page)
                total_pages = (total + per_page - 1) // per_page if total > 0 else 1
                if page > total_pages:
                    page = total_pages

                if not promos:
                    answer = "📭 Активных промокодов пока нет."
                    send_keyboard = get_main_menu_keyboard()
                else:
                    answer = f"📋 Активные промокоды (Стр. {page} из {total_pages}):\n\n"
                    for p in promos:
                        limit = f"{p['current_uses']}/{p['max_uses']}" if p[
                                                                              'max_uses'] is not None else f"{p['current_uses']}/∞"
                        answer += f"🔹 `{p['code']}` | Награда: {p['reward']} | Использовано: {limit}\n"
                    answer += "\n Чтобы создать новый: `/admin_promo_add КОД НАГРАДА [ЛИМИТ]`"
                    send_keyboard = get_admin_promo_list_keyboard(page, total_pages)

            elif command == "admin_tickets" or text_lower == "/admin_tickets":
                try:
                    page = int(payload.get("page", 1))
                    if len(parts) > 1 and text_lower.startswith("/admin_tickets"):
                        page = int(parts[1])
                    if page < 1:
                        page = 1
                except ValueError:
                    answer = "❌ Неверный формат."
                else:
                    limit = 4
                    tickets, total_count = await support_repo.get_tickets_paginated(page=page, limit=limit)
                    total_pages = (total_count + limit - 1) // limit if total_count > 0 else 1

                    if page > total_pages:
                        answer = f"❌ Страница {page} не существует. Всего страниц: {total_pages}"
                    elif not tickets:
                        answer = "📭 Обращений в поддержку пока нет."
                    else:
                        answer = f"🎫 Обращения (Стр. {page} из {total_pages}, всего: {total_count}):\n\n"
                        for t in tickets:
                            status_emoji = "🟢" if t["status"] == "open" else "✅"
                            ans_text = t["text"][:50] + "..." if len(t["text"]) > 50 else t["text"]
                            answer += (
                                f"{status_emoji} ID: {t['id']} | VK: {t['vk_user_id']}\n"
                                f"📝 {ans_text}\n"
                                f"📎 {'Есть скриншот' if t['attachment_url'] else 'Нет вложений'}\n"
                                f"🕒 {t['created_at']}\n\n"
                            )
                        send_keyboard = get_admin_ticket_list_keyboard(tickets, page, total_pages)

            elif command == "admin_ticket_view":
                ticket_id = payload.get("id")
                if not ticket_id and len(parts) >= 2:
                    try:
                        ticket_id = int(parts[1])
                    except ValueError:
                        pass

                if not ticket_id:
                    answer = "❌ Неверный ID. Используй кнопку или `/admin_ticket_view <id>`"
                else:
                    ticket = await support_repo.get_ticket_by_id(ticket_id)
                    if not ticket:
                        answer = f"❌ Тикет с ID {ticket_id} не найден."
                    else:
                        status_emoji = "🟢 Открыт" if ticket["status"] == "open" else "✅ Решен"
                        answer = (
                            f"🎫 Детали тикета #{ticket['id']} ({status_emoji})\n\n"
                            f"👤 Внутренний ID: {ticket['user_id']}\n"
                            f"🆔 VK ID: {ticket['vk_user_id']}\n"
                            f"🕒 Дата: {ticket['created_at']}\n\n"
                            f"💬 Текст обращения:\n{ticket['text'] or 'Без текста'}\n\n"
                        )
                        if ticket["attachment_url"]:
                            answer += f"📸 Скриншот: {ticket['attachment_url']}\n"
                        page = int(payload.get("page", 1))
                        send_keyboard = get_admin_ticket_actions_keyboard(ticket_id, page)

            elif command == "admin_ticket_resolve":
                ticket_id = payload.get("id")
                if not ticket_id and len(parts) >= 2:
                    try:
                        ticket_id = int(parts[1])
                    except ValueError:
                        pass

                if not ticket_id:
                    answer = "❌ Неверный ID. Используй кнопку или `/admin_ticket_resolve <id>`"
                else:
                    success = await support_repo.resolve_ticket(ticket_id)
                    if success:
                        answer = f"✅ Тикет #{ticket_id} успешно закрыт (статус: resolved)."
                    else:
                        answer = f"❌ Не удалось закрыть тикет #{ticket_id}. Возможно, он уже закрыт или не существует."
                    send_keyboard = get_admin_ticket_actions_keyboard(ticket_id, 1)

            else:
                answer = (
                    "🛠️ Доступные админ-команды:\n\n"
                    "`/admin_check <vk_id>` — узнать баланс и статистику\n"
                    "`/admin_add <vk_id> <кол-во>` — начислить энергию вручную\n"
                    "`/admin_reset <vk_id>` — сбросить историю пользователю\n"
                    "`/admin_refill_all <кол-во>` — 🚀 массово пополнить всем\n"
                    "`/admin_promo_add <код> <награда> [лимит]` — 🎁 создать промокод\n"
                    "`/admin_promo_list` — 📋 показать все активные промокоды\n"
                    "`/admin_tickets [стр]` — 🎫 список тикетов (с пагинацией)\n"
                    "`/admin_ticket_view <id>` — 👁️ полный просмотр тикета + скрин\n"
                    "`/admin_ticket_resolve <id>` — ✅ закрыть тикет"
                )

    # === ПЕРЕГЕНЕРАЦИЯ ОТВЕТА ===
    elif cmd == "regenerate":
        current_char = await char_repo.get_user_character(user["id"])
        is_admin = get_settings().is_admin(int(from_id))

        if not current_char:
            answer = "Сначала выбери персонажа! 👇"
            send_keyboard = get_main_menu_keyboard()
        else:
            balance = await payment_repo.get_user_balance(user["id"])
            target_model = settings.model

            if balance <= 0 and not is_admin:
                char_name = current_char["name"]
                answer = (
                    f"😿 {char_name} устала и ей нужно отдохнуть!\n\n"
                    f"Чтобы продолжить, пополни энергию или используй промокод (команда `/promo`).\n\n"
                )
                send_keyboard = get_payment_keyboard()
            else:
                history = await msg_repo.get_recent_history(user["id"], current_char["id"], limit=2)

                if len(history) < 2 or history[-1]["role"] != "assistant":
                    answer = "Нечего перегенерировать. Напиши что-нибудь первым! 😉"
                    send_keyboard = get_dialog_keyboard()
                else:
                    if not is_admin:
                        success = await payment_repo.deduct_messages(user["id"], 1)
                        if not success:
                            answer = "😿 Не удалось списать энергию. Попробуй позже."
                            send_keyboard = get_main_menu_keyboard()
                            await api.send_message(peer_id=int(peer_id), text=answer, keyboard=send_keyboard)
                            return

                    await msg_repo.delete_last_assistant_message(user["id"], current_char["id"])
                    last_user_text = history[-2]["content"]

                    await api.send_message(
                        peer_id=int(peer_id),
                        text="🔄 Перегенерация ответа..."
                    )
                    active_model = user.get("preferred_model") or target_model

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

    # === МАГИЯ РИСОВАНИЯ ===
    elif cmd == "generate_image":
        current_char = await char_repo.get_user_character(user["id"])

        if not current_char:
            answer = "Сначала выбери персонажа, чью магию ты хочешь увидеть! 👇"
            send_keyboard = get_main_menu_keyboard()
        else:
            await chat_queue.add(ChatTask(
                user_id=user["id"],
                char_id=current_char["id"],
                peer_id=int(peer_id),
                text="[GENERATE_IMAGE]",
                user_dict=user,
                char_dict=current_char,
                keyboard=get_regenerate_inline_keyboard(),
                is_image_generation=True
            ))
            return

    elif cmd == "copy_referral":
        code = payload.get("code", "")
        answer = (
            f"📋 **Твой реферальный код:**\n\n"
            f"`{code}`\n\n"
            f"Скопируй его и отправь другу. Пусть он введет его командой:\n"
            f"`/promo {code}`\n\n"
            f"🎁 **Бонусы за дружбу:**\n"
            f"• Твой друг получит **10 энергии** на старт!\n"
            f"• Ты получишь **5 энергии** за приглашение!\n\n"
            f"Бонусы начислятся автоматически, как только друг напишет мне первое сообщение. ⚡"
        )
        send_keyboard = get_main_menu_keyboard()

    # === ПРОМОКОДЫ И РЕФЕРАЛЫ ===
    elif cmd == "promo" or text_lower.startswith("/promo"):
        promo_code = ""
        if text_lower.startswith("/promo"):
            parts = text.split(maxsplit=1)
            promo_code = parts[1].strip() if len(parts) > 1 else ""

        if not promo_code:
            answer = "🎁 Введите промокод!\n\nНапишите команду в формате:\n/promo KITSUNE2026"
            send_keyboard = get_main_menu_keyboard()
        else:
            is_referral = False
            referrer_user = None

            if promo_code.lower().startswith("vk") and promo_code[2:].isdigit():
                referrer_vk_id = int(promo_code[2:])
                referrer_user = await user_repo.get_by_vk_id_strict(referrer_vk_id)
                if referrer_user:
                    is_referral = True

            if is_referral:
                if user["id"] == referrer_user["id"]:
                    answer = "😿 Ты не можешь пригласить сам себя!"
                elif user.get("referred_by"):
                    answer = "⚠️ Ты уже был приглашен другим пользователем."
                else:
                    created_at_str = user.get("created_at")
                    if created_at_str:
                        created_at_dt = datetime.strptime(created_at_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
                        now_dt = datetime.now()

                        if (now_dt - created_at_dt) > timedelta(hours=24):
                            answer = "⏳ Этот промокод предназначен только для новичков (до 24 часов в боте)."
                        else:
                            await user_repo.set_referred_by(user["id"], referrer_user["id"])
                            answer = (
                                f"✅ Промокод принят! Ты приглашен пользователем {referrer_vk_id}.\n\n"
                                f"Напиши мне любое сообщение, чтобы активировать бонус (тебе 10 энергии, другу 5)!"
                            )
                    else:
                        await user_repo.set_referred_by(user["id"], referrer_user["id"])
                        answer = "✅ Промокод принят! Напиши что-нибудь, чтобы получить бонус."
            else:
                promo = await promo_repo.get_active_promo(promo_code.upper())
                if not promo:
                    answer = f"❌ Промокод {promo_code} не найден или уже недействителен."
                elif await promo_repo.has_user_used_promo(user["id"], promo["id"]):
                    answer = f"⚠️ Вы уже использовали промокод {promo_code}."
                else:
                    reward = promo["reward"]
                    await payment_repo.add_user_messages(user["id"], reward)
                    await promo_repo.apply_promo_code(user["id"], promo["id"])
                    new_balance = await payment_repo.get_user_balance(user["id"])
                    answer = f"🎉 Промокод {promo_code} активирован!\n\n⚡ Начислено: {reward} энергии\n💬 Текущий баланс: {new_balance} энергии"

            send_keyboard = get_main_menu_keyboard()

    # === ОБЫЧНЫЙ ДИАЛОГ С ПЕРСОНАЖЕМ ===
    else:
        if user.get("referred_by") and not user.get("referral_bonus_claimed"):
            await payment_repo.add_user_messages(user["id"], 10)
            await payment_repo.add_user_messages(user["referred_by"], 5)
            await user_repo.mark_referral_bonus_claimed(user["id"])
            logger.info(f"Referral bonus awarded to user {user['id']} and referrer {user['referred_by']}")

        user_ts = awaiting_support.get(int(from_id))

        if user_ts and (time.time() - user_ts <= 300):
            del awaiting_support[int(from_id)]
            attachments = message.get("attachments", [])
            if not text and not attachments:
                answer = "😿 Ты ничего не написал и не прикрепил скриншот. Попробуй еще раз или нажми кнопку '🆘 Поддержка' в главном меню."
                send_keyboard = get_main_menu_keyboard()
            else:
                from app.services.support import process_support_request
                answer = await process_support_request(
                    api=api,
                    user_id=user["id"],
                    vk_user_id=int(from_id),
                    text=text,
                    attachments=attachments,
                    support_repo=support_repo
                )
                send_keyboard = get_main_menu_keyboard()

            await api.send_message(
                peer_id=int(peer_id),
                text=answer,
                keyboard=send_keyboard,
                attachment=attachment,
            )
            return

        if cmd and cmd != "support":
            awaiting_support.pop(int(from_id), None)

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
                is_admin = get_settings().is_admin(int(from_id))

                if balance <= 0 and not is_admin:
                    char_name = current_char["name"]
                    answer = (
                        f"😿 {char_name} устала и ей нужно отдохнуть!\n\n"
                        f"Чтобы продолжить, пополни энергию или используй промокод (команда `/promo`).\n\n"
                        f"Пока я восстанавливаюсь, можешь заглянуть к моим друзьям:\n"
                        f"🎨 Генератор идеальных вайфу: @MyNekoBaka89_bot\n"
                        f"💻 Шира-тян — твоя ИИ-ассистентка: @shira_neuro_bot"
                    )
                    send_keyboard = get_payment_keyboard()
                    await api.send_message(peer_id=int(peer_id), text=answer, keyboard=send_keyboard)
                    return

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

        if not text and not photo_url:
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

                is_admin = get_settings().is_admin(int(from_id))
                if balance <= 0 and not is_admin:
                    char_name = current_char["name"]
                    answer = (
                        f"😿 {char_name} устала и ей нужно отдохнуть!\n\n"
                        f"Чтобы продолжить, пополни энергию или используй промокод (команда `/promo`).\n\n"
                        f"Пока я восстанавливаюсь, можешь заглянуть к моим друзьям:\n"
                        f"🎨 Генератор идеальных вайфу: @MyNekoBaka89_bot\n"
                        f"💻 Шира-тян — твоя ИИ-ассистентка: @shira_neuro_bot"
                    )
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
                    model_name=active_model,
                    photo_url=photo_url
                ))
                return

    # ЕДИНСТВЕННАЯ отправка системного ответа в конце
    await api.send_message(
        peer_id=int(peer_id),
        text=answer,
        keyboard=send_keyboard,
        attachment=attachment,
    )