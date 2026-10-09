#!/usr/bin/env python3
"""
Скрипт восстановления неотвеченных сообщений.
Версия 2: Группирует по пользователям, чтобы не создавать спам.
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

    async with aiohttp.ClientSession() as session:
        api = VKApi(session, settings.group_token)

        # 🔥 ИСПРАВЛЕНИЕ: Группируем по user_id и character_id.
        # Берем только MAX(id), то есть последнее неотвеченное сообщение от каждого юзера.
        query = """
            SELECT MAX(m.id) as msg_id, m.user_id, m.character_id, u.vk_user_id, c.name as char_name
            FROM messages m
            JOIN users u ON m.user_id = u.id
            LEFT JOIN characters c ON m.character_id = c.id
            WHERE m.role = 'user'
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
            logger.info("✅ Все сообщения обработаны. Нечего восстанавливать.")
            return

        logger.warning(f"⚠️ НАЙДЕНО {len(rows)} ПОЛЬЗОВАТЕЛЕЙ С ПРОПУЩЕННЫМИ СООБЩЕНИЯМИ. ЗАПУСКАЕМ СПАСЕНИЕ...")

        for row in rows:
            vk_peer_id = row['vk_user_id']
            char_name = row['char_name'] or "Персонаж"

            # 🔥 ЕДИНОЕ сообщение-восстановление вместо спама
            safe_answer = f"*{char_name} моргает, словно выходя из транса, и виновато улыбается.* Прости, я немного зависла и пропустила твои сообщения. Я здесь! Напомни, на чем мы остановились?"

            try:
                await api.send_message(peer_id=vk_peer_id, text=safe_answer)
                logger.info(f"✅ Отправлено одно сообщение-восстановление пользователю {vk_peer_id}")

                # Закрываем цикл в БД, чтобы скрипт не нашел их снова
                insert_query = """
                    INSERT INTO messages (user_id, character_id, role, content)
                    VALUES (?, ?, 'assistant', ?);
                """
                await conn.execute(insert_query, (row['user_id'], row['character_id'], safe_answer))
                await conn.commit()

            except Exception as e:
                logger.error(f"❌ Ошибка при восстановлении для {vk_peer_id}: {e}")

    logger.info("🏁 Процесс восстановления завершен.")


if __name__ == "__main__":
    asyncio.run(recover())