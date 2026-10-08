#!/usr/bin/env python3
"""
Бэкап базы данных: отправка файла админу в личные сообщения ВКонтакте.
"""
import os
import sqlite3
import logging
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# 🔥 ЗАГРУЖАЕМ .env (переменные не меняем, они работают!)
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Используем те же имена, которые успешно сработали
VK_TOKEN = os.getenv("VK_BOT_GROUP_TOKEN") or os.getenv("VK_GROUP_TOKEN")
ADMIN_VK_ID = os.getenv("VK_BOT_SUPPORT_ADMIN_ID")

API_VERSION = "5.199"
DB_PATH = Path("data/bot.db")
VK_API_URL = "https://api.vk.com/method"

if not VK_TOKEN or not ADMIN_VK_ID:
    logger.error("❌ ОШИБКА: Не найдены токены или ID в файле .env!")
    exit(1)


def vacuum_db(db_path: Path, output_path: Path) -> None:
    logger.info(f"🗄️ VACUUM INTO: {db_path} -> {output_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"VACUUM INTO '{output_path}'")
    finally:
        conn.close()
    logger.info(f"✅ Backup created: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")


def send_to_vk_admin(file_path: Path) -> bool:
    """Загружает документ в ВК и отправляет админу."""
    # 1. Получаем URL для загрузки
    upload_url_req = requests.get(
        f"{VK_API_URL}/docs.getMessagesUploadServer",
        params={"type": "doc", "peer_id": ADMIN_VK_ID, "v": API_VERSION, "access_token": VK_TOKEN}
    ).json()

    if "error" in upload_url_req:
        logger.error(f"❌ VK API Error (get URL): {upload_url_req['error']}")
        return False

    upload_url = upload_url_req["response"]["upload_url"]

    # 2. Загружаем файл
    with open(file_path, "rb") as f:
        upload_response = requests.post(upload_url, files={"file": f})
        try:
            upload_req = upload_response.json()
        except requests.exceptions.JSONDecodeError:
            logger.error(f"❌ VK Upload вернул не JSON: {upload_response.text}")
            return False

    if "error" in upload_req or "file" not in upload_req:
        logger.error(f"❌ VK Upload Error: {upload_req}")
        return False

    file_param = upload_req["file"]

    # 3. Сохраняем документ
    save_req = requests.post(
        f"{VK_API_URL}/docs.save",
        params={"file": file_param, "v": API_VERSION, "access_token": VK_TOKEN}
    ).json()

    if "error" in save_req:
        logger.error(f"❌ VK API Error (save): {save_req['error']}")
        return False

    # 🔥 ИСПРАВЛЕНИЕ: Для сообщений VK возвращает словарь с ключом 'doc', а не список!
    response_data = save_req.get("response")
    if not response_data or not isinstance(response_data, dict) or "doc" not in response_data:
        logger.error(f"❌ VK API вернул неожиданный формат при сохранении: {save_req}")
        return False

    doc = response_data["doc"]
    owner_id = doc["owner_id"]
    doc_id = doc["id"]
    logger.info(f"✅ Документ сохранен: doc{owner_id}_{doc_id}")

    # 4. Отправляем сообщение с вложением
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    size_kb = file_path.stat().st_size / 1024

    msg_req = requests.post(
        f"{VK_API_URL}/messages.send",
        params={
            "user_id": ADMIN_VK_ID,
            "message": f"🗄️ Ежедневный бэкап VK Bot\n🕒 {timestamp}\n📊 Размер: {size_kb:.1f} KB",
            "attachment": f"doc{owner_id}_{doc_id}",
            "random_id": 0,
            "v": API_VERSION,
            "access_token": VK_TOKEN
        }
    ).json()

    if "error" in msg_req:
        logger.error(f"❌ VK API Error (send): {msg_req['error']}")
        return False

    logger.info("📤 Successfully sent to VK Admin!")
    return True


def main() -> None:
    if not DB_PATH.exists():
        logger.error(f"❌ DB not found: {DB_PATH}")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_backup = Path(f"/tmp/bot_backup_{timestamp}.db")

    vacuum_db(DB_PATH, temp_backup)

    try:
        success = send_to_vk_admin(temp_backup)
        if success:
            logger.info("🎉 VK Backup completed successfully!")
        else:
            logger.error("❌ VK Backup failed!")
    finally:
        if temp_backup.exists():
            temp_backup.unlink()


if __name__ == "__main__":
    main()