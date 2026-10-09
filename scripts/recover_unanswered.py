#!/usr/bin/env python3
"""
Скрипт восстановления неотвеченных сообщений (для Cron).
Версия 3: С защитой от времени (не трогает свежие сообщения).
"""
import asyncio
import logging
import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

from app.db.connection import Database
from app.vk.api import VKApi
from app.config import get_settings
import aiohttp

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("RECOVERY")


async def recover():
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    conn = db.connection

    try:
        async with aiohttp.ClientSession() as session:
            api = VKApi(session, settings.group_token)

            # 🔥 ИСПРАВЛЕНИЕ: Добавили проверку времени!
            # Скрипт трогает только те сообщения, которым больше 2 минут.
            # Если бот просто "думает" (генерирует ответ), скрипт его не тронет.
            query = """
                SELECT MAX(m.id) as msg_id, m.user_id, m.character_id, u.vk_user_id, c.name as char_name
                FROM messages m
                JOIN users u ON m.user_id = u.id
                LEFT JOIN characters c ON m.character_id = c.id
                WHERE m.role = 'user'
                  AND m.created_at < datetime('now', '-30 minutes')
                  AND NOT EXISTS (
                      SELECT 1 FROM messages m2 
                      WHERE m2.user_id = m.user_id 
                        AND m2.character_id = m.character_id 
                        AND m2.role = 'assistant' 
                        AND m2.id > m.id
                  )
                GROUP BY m.user_id, m.character_id
                LIMIT 30;
            """

            cursor = await conn.execute(query)
            rows = await cursor.fetchall()
            if not rows:
                # Логируем только если есть что показать, чтобы не спамить в cron
                return

            logger.warning(f"⚠️ НАЙДЕНО {len(rows)} ЗАВИСШИХ ПОЛЬЗОВАТЕЛЕЙ (>2 мин). ЗАПУСКАЕМ СПАСЕНИЕ...")

            for row in rows:
                vk_peer_id = row['vk_user_id']
                char_name = row['char_name'] or "Персонаж"

                safe_answer = f"*{char_name} моргает, словно выходя из транса, и виновато улыбается.* Прости, связь немного прервалась. Я здесь! Напомни, на чем мы остановились?"

                try:
                    await api.send_message(peer_id=vk_peer_id, text=safe_answer)
                    logger.info(f"✅ Восстановлено для {vk_peer_id}")

                    insert_query = "INSERT INTO messages (user_id, character_id, role, content) VALUES (?, ?, 'assistant', ?);"
                    await conn.execute(insert_query, (row['user_id'], row['character_id'], safe_answer))
                    await conn.commit()

                except Exception as e:
                    if "901" not in str(e):  # Игнорируем ошибки блокировки
                        logger.error(f"❌ Ошибка для {vk_peer_id}: {e}")
    finally:
        await conn.close()

    logger.info("🏁 Процесс восстановления завершен.")


if __name__ == "__main__":
    asyncio.run(recover())