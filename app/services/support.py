# app/services/support.py
import logging
from typing import List, Dict, Any
from app.config import get_settings
from app.db.repositories.support import SupportRepository

logger = logging.getLogger(__name__)

async def process_support_request(
        api,
        user_id: int,
        vk_user_id: int,
        text: str,
        attachments: List[Dict[str, Any]],
        support_repo: SupportRepository
) -> str:
    settings = get_settings()

    admin_id_str = settings.admin_ids.split(",")[0].strip() if settings.admin_ids else ""
    try:
        admin_id = int(admin_id_str)
    except ValueError:
        logger.error("Valid admin_id not found in settings!")
        return "😿 Извини, сейчас служба поддержки недоступна. Попробуй позже."

    # 1. Извлекаем URL первого прикрепленного фото (если есть)
    attachment_url = None
    if attachments:
        for att in attachments:
            if att.get("type") == "photo":
                sizes = att["photo"].get("sizes", [])
                if sizes:
                    attachment_url = sizes[-1].get("url")
                break

    # 2. Сохраняем в БД через переданный репозиторий
    await support_repo.create_ticket(user_id, vk_user_id, text, attachment_url)

    # 3. Уведомляем админа (БЕЗ HTML ТЕГОВ!)
    admin_msg = (
        f"🆘 Новое обращение в поддержку!\n\n"
        f"VK ID: {vk_user_id}\n"
        f"Текст: {text or 'Без текста, только скриншот'}\n"
    )

    try:
        if attachment_url:
            await api.send_message(
                peer_id=admin_id,
                text=f"{admin_msg}\nСкриншот: {attachment_url}"
            )
        else:
            await api.send_message(peer_id=admin_id, text=admin_msg)

        logger.info("✅ Support ticket forwarded to admin %s", admin_id)
    except Exception as e:
        logger.error("Failed to notify admin about support ticket: %s", e)

    # 4. Ответ пользователю
    return (
        "✅ Твое обращение принято!\n\n"
        "Мы сохранили его и передали разработчику. Если ты прикрепил скриншот, мы его тоже получили. "
        "Постараемся решить проблему как можно скорее! 🐾"
    )