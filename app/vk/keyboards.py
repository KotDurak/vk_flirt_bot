from app.vk.keyboard import KeyboardBuilder
import json

def get_main_menu_keyboard() -> str:
    """Главное меню бота."""
    kb = KeyboardBuilder(one_time=False)

    # Основные действия
    kb.add_button("👤 Выбрать персонажа", payload={"cmd": "chars"}, color="primary")
    kb.row()
    kb.add_button("⚡ Купить энергию", payload={"cmd": "buy"}, color="positive")
    kb.row()

    # Второстепенные действия
    kb.add_button("📊 Мой профиль", payload={"cmd": "profile"}, color="secondary")
    kb.add_button("🎁 Ввести промокод", payload={"cmd": "promo"}, color="secondary")
    kb.row()

    # 🆕 НОВАЯ КНОПКА РЕФЕРАЛЬНОЙ СИСТЕМЫ
    kb.add_button("💸 Заработать энергию", payload={"cmd": "referral"}, color="secondary")
    kb.row()

    # Служебные действия
    kb.add_button("ℹ️ Помощь", payload={"cmd": "help"}, color="secondary")
    kb.add_button("🆘 Поддержка", payload={"cmd": "support"}, color="secondary")
    kb.row()

    # Опасное действие
    kb.add_button("🔄 Сбросить диалог", payload={"cmd": "reset"}, color="negative")

    return kb.to_json()


def get_admin_promo_list_keyboard(page: int, total_pages: int) -> str:
    """Клавиатура для пагинации списка промокодов."""
    kb = KeyboardBuilder(one_time=False, inline=True)

    if page > 1:
        kb.add_button("⬅️ Назад", payload={"cmd": "admin_promo_list", "page": page - 1}, color="secondary")
    if page < total_pages:
        kb.add_button("Вперед ➡️", payload={"cmd": "admin_promo_list", "page": page + 1}, color="secondary")

    kb.row()
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")
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


def get_admin_ticket_list_keyboard(tickets: list[dict], page: int, total_pages: int) -> str:
    """Клавиатура списка тикетов - только просмотр, закрытие внутри тикета."""
    kb = KeyboardBuilder(one_time=False, inline=True)

    # Кнопки просмотра (по 2 в ряд для компактности)
    for i, t in enumerate(tickets):
        tid = t['id']
        kb.add_button(f"👁️ #{tid}", payload={"cmd": "admin_ticket_view", "id": tid}, color="primary")
        # Переход на новый ряд после каждых 2 кнопок
        if (i + 1) % 2 == 0:
            kb.row()

    # Если последний ряд неполный - завершаем его
    if len(tickets) % 2 != 0:
        kb.row()

    # Навигация по страницам
    if page > 1 or page < total_pages:
        if page > 1:
            kb.add_button("⬅️", payload={"cmd": "admin_tickets", "page": page - 1}, color="secondary")
        if page < total_pages:
            kb.add_button("➡️", payload={"cmd": "admin_tickets", "page": page + 1}, color="secondary")
        kb.row()

    kb.add_button(" В меню", payload={"cmd": "start"}, color="secondary")
    return kb.to_json()


def get_admin_ticket_actions_keyboard(ticket_id: int, page: int = 1) -> str:
    """Кнопки действий для конкретного тикета."""
    kb = KeyboardBuilder(one_time=False, inline=True)

    # Кнопки действий с тикетом
    kb.add_button("👁️ Просмотреть детали", payload={"cmd": "admin_ticket_view", "id": ticket_id}, color="primary")
    kb.add_button("✅ Закрыть тикет", payload={"cmd": "admin_ticket_resolve", "id": ticket_id}, color="positive")

    kb.row()
    # Навигация
    kb.add_button("⬅️ К списку", payload={"cmd": "admin_tickets", "page": page}, color="secondary")
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")

    return kb.to_json()


def get_referral_keyboard(referral_code: str) -> str:
    """Клавиатура для шеринга реферального кода."""
    kb = KeyboardBuilder(one_time=False, inline=True)

    # Кнопка, которая попросит бота продублировать код текстом для удобного копирования
    kb.add_button(
        label=f"📋 Скопировать код: {referral_code}",
        payload={"cmd": "copy_referral", "code": referral_code},
        color="primary"
    )

    kb.row()
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")

    return kb.to_json()