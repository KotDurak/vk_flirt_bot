# app/vk/keyboard.py
import json
from typing import Any


class KeyboardBuilder:
    """
    Конструктор клавиатур для ВКонтакте.

    Цвета кнопок:
    - primary (синий)
    - secondary (белый/серый)
    - negative (красный)
    - positive (зеленый)
    """

    def __init__(self, one_time: bool = False, inline: bool = False) -> None:
        self.one_time = one_time
        self.inline = inline
        self.rows: list[list[dict[str, Any]]] = []
        self.current_row: list[dict[str, Any]] = []

    def add_button(
            self,
            label: str,
            payload: dict[str, Any] | None = None,
            color: str = "primary"
    ) -> "KeyboardBuilder":
        """Добавляет кнопку в текущий ряд."""
        button: dict[str, Any] = {
            "action": {
                "type": "text",
                "label": label,
            },
            "color": color
        }

        # Payload нужен, чтобы бот понимал, какая именно кнопка нажата,
        # даже если текст на кнопке изменится или будет длинным.
        if payload:
            button["action"]["payload"] = json.dumps(payload, ensure_ascii=False)

        self.current_row.append(button)
        return self

    def row(self) -> "KeyboardBuilder":
        """Завершает текущий ряд и начинает новый."""
        if self.current_row:
            self.rows.append(self.current_row)
            self.current_row = []
        return self

    def to_json(self) -> str:
        if self.current_row:
            self.rows.append(self.current_row)

        keyboard = {
            "one_time": self.one_time,
            "buttons": self.rows,
        }
        if self.inline:
            keyboard["inline"] = True

        # Убираем ensure_ascii=False, чтобы JSON был чистым ASCII
        return json.dumps(keyboard)