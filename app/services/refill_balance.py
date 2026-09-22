#!/usr/bin/env python3
"""
refill_balance.py
Автономная утилита для пополнения баланса пользователей.
Лежит в app/services/, работает с базой data/bot.db.
Не импортирует логику бота, полностью безопасен для concurrent-доступа.
"""

import asyncio
import aiosqlite
import sys
import logging
from pathlib import Path
from datetime import datetime

# Настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ==================== НАСТРОЙКИ ====================

# Автоматически вычисляем путь к базе данных:
# __file__ -> app/services/refill_balance.py
# .parents[2] -> корень проекта
# / "data" / "bot.db" -> итоговый путь
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "bot.db"

TABLE_NAME = "users"
BALANCE_COLUMN = "messages"

# Если у тебя есть колонка is_active, раскомментируй строку ниже:
# ACTIVE_CONDITION = "AND is_active = 1"
ACTIVE_CONDITION = ""


# ===================================================

async def refill_balances(target_balance: int):
    if not DB_PATH.exists():
        logger.error(f"❌ Файл базы данных не найден по пути: {DB_PATH}")
        sys.exit(1)

    logger.info(f"Подключение к базе данных: {DB_PATH}")

    try:
        # Используем те же безопасные настройки, что и в твоём классе Database
        async with aiosqlite.connect(
                DB_PATH,
                isolation_level="IMMEDIATE",  # Предотвращает deadlock при конкурентной записи
                timeout=10.0  # Ждет до 10 секунд, если база занята ботом
        ) as conn:

            # Убеждаемся, что мы в том же режиме, что и бот (безопасно для concurrent доступа)
            await conn.execute("PRAGMA journal_mode=WAL;")

            # Формируем запрос
            query = f"""
                UPDATE {TABLE_NAME} 
                SET {BALANCE_COLUMN} = ? 
                WHERE {BALANCE_COLUMN} < ?
                {ACTIVE_CONDITION}
            """

            cursor = await conn.execute(query, (target_balance, target_balance))
            await conn.commit()

            affected_rows = cursor.rowcount
            logger.info(f"✅ Успешно пополнено {affected_rows} пользователей до {target_balance} сообщений.")

            if affected_rows == 0:
                logger.info("ℹ️ Нет пользователей с балансом меньше указанного. Никто не пострадал.")

    except aiosqlite.OperationalError as e:
        logger.error(f"❌ Ошибка базы данных: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        sys.exit(1)


def main():
    # Парсинг аргументов командной строки
    if len(sys.argv) > 1:
        try:
            target = int(sys.argv[1])
            if target <= 0:
                raise ValueError
        except ValueError:
            logger.error(
                "Ошибка: укажите корректное положительное число. Пример: python -m app.services.refill_balance 50")
            sys.exit(1)
    else:
        target = 50  # Значение по умолчанию

    logger.info(f"🎯 Целевой баланс: {target} сообщений")

    # Запуск асинхронной функции
    asyncio.run(refill_balances(target))


if __name__ == "__main__":
    main()