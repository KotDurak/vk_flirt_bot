from app.vk.keyboard import KeyboardBuilder
import json


def get_main_menu_keyboard() -> str:
    """Главное меню бота (оптимизировано в 2 колонки)."""
    kb = KeyboardBuilder(one_time=False)

    # Основные действия (2 в ряд)
    kb.add_button("👤 Персонажи", payload={"cmd": "chars"}, color="primary")
    kb.add_button("⚡ Купить энергию", payload={"cmd": "buy"}, color="positive")
    kb.row()

    # Второстепенные действия (2 в ряд)
    kb.add_button("📊 Мой профиль", payload={"cmd": "profile"}, color="secondary")
    kb.add_button("🎁 Ввести промокод", payload={"cmd": "promo"}, color="secondary")
    kb.row()

    # 🔥 НОВЫЙ БЛОК: Ами + Рефералка (2 в ряд)
    kb.add_button("💬 Ами (Поддержка)", payload={"cmd": "ami_support"}, color="secondary")
    kb.add_button("💸 Заработать энергию", payload={"cmd": "referral"}, color="secondary")
    kb.row()

    # Служебные действия (2 в ряд)
    kb.add_button("ℹ️ Помощь", payload={"cmd": "help"}, color="secondary")
    kb.add_button("🔄 Сбросить диалог", payload={"cmd": "reset"}, color="negative")
    kb.row()

    # Отдельная кнопка для создания тикета (чтобы не путать с болталкой с Ами)
    kb.add_button("🆘 Сообщить о проблеме", payload={"cmd": "support"}, color="secondary")
    kb.row()

    # === Кнопка-ссылка на Mini App (каталог персонажей) ===
    keyboard_json = json.loads(kb.to_json())
    keyboard_json["buttons"].append([
        {
            "action": {
                "type": "open_link",
                "link": "https://vk.ru/app54816075",
                "label": "🎭 Открыть каталог с фильтрами"
            }
        }
    ])
    return json.dumps(keyboard_json)


def get_no_energy_keyboard() -> str:
    """🔥 НОВАЯ ФУНКЦИЯ: Клавиатура, когда у пользователя закончилась энергия."""
    kb = KeyboardBuilder(one_time=False, inline=True)  # inline выглядит аккуратнее под сообщением

    kb.add_button("⚡ Купить энергию", payload={"cmd": "buy"}, color="positive")
    kb.row()
    kb.add_button("💬 Поговорить с Ами (бесплатно)", payload={"cmd": "ami_support"}, color="secondary")
    kb.row()
    kb.add_button("🎁 Ввести промокод", payload={"cmd": "promo"}, color="secondary")

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


def get_regenerate_inline_keyboard(can_generate_image) -> str:
    """Только кнопки, прикрепленные к сообщению, вертикально."""
    kb = KeyboardBuilder(one_time=False, inline=True)

    kb.add_button("🔄 Перегенерировать (-1 ⚡)", payload={"cmd": "regenerate"}, color="secondary")
    kb.row()
    if can_generate_image:
        kb.add_button("🎨 Нарисовать (5 ⚡)", payload={"cmd": "generate_image"}, color="primary")

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
        if (i + 1) % per_row == 0:
            kb.row()

    kb.row()
    kb.add_button("⬅️ В главное меню", payload={"cmd": "start"}, color="secondary")
    return kb.to_json()


def get_characters_paginated_keyboard(characters: list[dict], page: int, total_pages: int, per_row: int = 2) -> str:
    """Создает инлайн-клавиатуру со списком персонажей с пагинацией."""
    kb = KeyboardBuilder(one_time=False, inline=True)

    for i, char in enumerate(characters):
        label = char["name"][:22] + "..." if len(char["name"]) > 22 else char["name"]
        kb.add_button(
            label=label,
            payload={"cmd": "select_char", "char_id": char["id"]},
            color="primary"
        )
        if (i + 1) % per_row == 0:
            kb.row()

    kb.row()
    if page > 1:
        kb.add_button("⬅️ Назад", payload={"cmd": "chars", "page": page - 1}, color="secondary")
    if page < total_pages:
        kb.add_button("Вперед ➡️", payload={"cmd": "chars", "page": page + 1}, color="secondary")

    kb.row()
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")

    keyboard_json = json.loads(kb.to_json())
    keyboard_json["buttons"].append([
        {
            "action": {
                "type": "open_link",
                "link": "https://vk.ru/app54816075",
                "label": "🎭 Открыть каталог с фильтрами"
            }
        }
    ])
    return json.dumps(keyboard_json)


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
                        "link": payment_url,
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
    """Клавиатура списка тикетов."""
    kb = KeyboardBuilder(one_time=False, inline=True)

    for i, t in enumerate(tickets):
        tid = t['id']
        kb.add_button(f"👁️ #{tid}", payload={"cmd": "admin_ticket_view", "id": tid}, color="primary")
        if (i + 1) % 2 == 0:
            kb.row()

    if len(tickets) % 2 != 0:
        kb.row()

    if page > 1 or page < total_pages:
        if page > 1:
            kb.add_button("⬅️", payload={"cmd": "admin_tickets", "page": page - 1}, color="secondary")
        if page < total_pages:
            kb.add_button("➡️", payload={"cmd": "admin_tickets", "page": page + 1}, color="secondary")
        kb.row()

    kb.add_button("🏠 В меню", payload={"cmd": "start"}, color="secondary")
    return kb.to_json()


def get_admin_ticket_actions_keyboard(ticket_id: int, page: int = 1) -> str:
    """Кнопки действий для конкретного тикета."""
    kb = KeyboardBuilder(one_time=False, inline=True)

    kb.add_button("👁️ Просмотреть детали", payload={"cmd": "admin_ticket_view", "id": ticket_id}, color="primary")
    kb.add_button("✅ Закрыть тикет", payload={"cmd": "admin_ticket_resolve", "id": ticket_id}, color="positive")

    kb.row()
    kb.add_button("⬅️ К списку", payload={"cmd": "admin_tickets", "page": page}, color="secondary")
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")

    return kb.to_json()


def get_referral_keyboard(referral_code: str) -> str:
    """Клавиатура для шеринга реферального кода."""
    kb = KeyboardBuilder(one_time=False, inline=True)

    kb.add_button(
        label=f"📋 Скопировать код: {referral_code}",
        payload={"cmd": "copy_referral", "code": referral_code},
        color="primary"
    )

    kb.row()
    kb.add_button("🏠 В главное меню", payload={"cmd": "start"}, color="secondary")

    return kb.to_json()