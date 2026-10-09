#!/usr/bin/env python3
"""
Скрипт восстановления неотвеченных сообщений.
Финальная версия с корректной инициализацией VKApi и БД.
"""
import asyncio
import logging
import sys
import os

# Добавляем корень проекта в пути Python
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

    # Инициализация БД
    db = Database(settings.db_path)
    await db.connect()
    conn = db.connection

    # 🔥 ПРАВИЛЬНАЯ ИНИЦИАЛИЗАЦИЯ VKApi: session ПЕРВЫМ аргументом
    async with aiohttp.ClientSession() as session:
        api = VKApi(session, settings.group_token)

        # Запрос ищет сообщения пользователя, после которых НЕТ ответа assistant
        query = """
            SELECT m.id as msg_id, m.user_id, m.character_id, m.content, u.vk_user_id, c.name as char_name
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
            ORDER BY m.id DESC
            LIMIT 30;
        """

        cursor = await conn.execute(query)
        rows = await cursor.fetchall()

        if not rows:
            logger.info("✅ Все сообщения обработаны. Нечего восстанавливать.")
            return

        logger.warning(f"⚠️ НАЙДЕНО {len(rows)} ПРОПУЩЕННЫХ СООБЩЕНИЙ. ЗАПУСКАЕМ СПАСЕНИЕ...")

        for row in rows:
            vk_peer_id = row['vk_user_id']
            user_text = row['content'][:40] + "..." if len(row['content']) > 40 else row['content']
            char_name = row['char_name'] or "Персонаж"

            safe_answer = f"*{char_name} вздыхает, потирая виски, словно вернувшись из глубоких раздумий.* Прости, я немного отвлеклась. Ты говорил что-то важное про «{user_text}»? Продолжай, я внимательно слушаю."

            try:
                # Отправляем сообщение пользователю
                await api.send_message(peer_id=vk_peer_id, text=safe_answer)
                logger.info(f"✅ Отправлен ответ пользователю {vk_peer_id}")

                # Закрываем цикл в БД
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