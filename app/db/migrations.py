# app/db/migrations.py
import logging
from app.db.connection import Database

logger = logging.getLogger(__name__)


async def run_migrations(db: Database) -> None:
    """Создает необходимые таблицы, если их нет."""
    conn = db.connection

    logger.info("Running database migrations...")

    # === ТАБЛИЦА ПОЛЬЗОВАТЕЛЕЙ ===
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vk_user_id INTEGER NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_premium BOOLEAN DEFAULT FALSE,
            messages INTEGER DEFAULT 80
        );
    """)

    # Добавляем поле messages, если его нет (для существующих БД)
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN messages INTEGER DEFAULT 10")
        logger.info("Added 'messages' column to users table")
    except Exception:
        pass  # Колонка уже существует

    # Индексы для быстрого поиска
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_vk_id ON users(vk_user_id);
    """)

    # === ТАБЛИЦА ПЕРСОНАЖЕЙ ===
    await conn.execute("""
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                photo_attachment TEXT,
                system_prompt TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                position INTEGER DEFAULT 0
            );
        """)

    # === СВЯЗЬ ПОЛЬЗОВАТЕЛЬ-ПЕРСОНАЖ ===
    await conn.execute("""
           CREATE TABLE IF NOT EXISTS user_character (
               user_id INTEGER NOT NULL,
               character_id INTEGER NOT NULL,
               selected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (user_id),
               FOREIGN KEY (user_id) REFERENCES users(id),
               FOREIGN KEY (character_id) REFERENCES characters(id)
           );
       """)

    await conn.execute("""
           CREATE INDEX IF NOT EXISTS idx_user_character_user_id ON user_character(user_id);
       """)

    # === ТАБЛИЦА СООБЩЕНИЙ ===
    await conn.execute("""
           CREATE TABLE IF NOT EXISTS messages (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER NOT NULL,
               character_id INTEGER NOT NULL,
               role TEXT NOT NULL,
               content TEXT NOT NULL,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               FOREIGN KEY (user_id) REFERENCES users(id),
               FOREIGN KEY (character_id) REFERENCES characters(id)
           );
       """)

    # === ТАБЛИЦА SUMMARY ===
    await conn.execute("""
           CREATE TABLE IF NOT EXISTS conversation_summary (
               user_id INTEGER NOT NULL,
               character_id INTEGER NOT NULL,
               summary TEXT NOT NULL,
               last_summarized_message_id INTEGER DEFAULT 0,
               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (user_id, character_id)
           );
       """)

    # === ТАБЛИЦА ПЛАТЕЖЕЙ ===
    await conn.execute("""
           CREATE TABLE IF NOT EXISTS payments (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER NOT NULL,
               invoice_id TEXT NOT NULL UNIQUE,
               amount INTEGER NOT NULL,
               messages INTEGER NOT NULL,
               status TEXT DEFAULT 'pending',
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               paid_at TIMESTAMP,
               FOREIGN KEY (user_id) REFERENCES users(id)
           );
       """)

    # Индексы для быстрого доступа
    await conn.execute("""
           CREATE INDEX IF NOT EXISTS idx_messages_user_char 
           ON messages(user_id, character_id, id);
       """)

    await conn.execute("""
           CREATE INDEX IF NOT EXISTS idx_summary_user_char 
           ON conversation_summary(user_id, character_id);
       """)

    await conn.execute("""
           CREATE INDEX IF NOT EXISTS idx_payments_invoice 
           ON payments(invoice_id);
       """)

    await conn.execute("""
           CREATE INDEX IF NOT EXISTS idx_payments_user_status 
           ON payments(user_id, status);
       """)

    await conn.commit()
    logger.info("Migrations completed successfully")