import logging
from app.db.connection import Database

logger = logging.getLogger(__name__)

async def run_migrations(db: Database) -> None:
    """Создает необходимые таблицы с правильной структурой."""
    conn = db.connection

    logger.info("Running database migrations...")

    # === ТАБЛИЦА ПОЛЬЗОВАТЕЛЕЙ (Сразу с preferred_model) ===
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vk_user_id INTEGER NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_premium BOOLEAN DEFAULT FALSE,
            messages INTEGER DEFAULT 80,
            preferred_model TEXT
        );
    """)

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
            position INTEGER DEFAULT 0,
            greeting_message TEXT
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

    # === ИНДЕКСЫ ===
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

    # === ТАБЛИЦА ПРОМОКОДОВ ===
    await conn.execute("""
           CREATE TABLE IF NOT EXISTS promo_codes (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               code TEXT NOT NULL UNIQUE,
               reward INTEGER NOT NULL,
               max_uses INTEGER,  -- ✅ Теперь может быть NULL (безлимит)
               current_uses INTEGER DEFAULT 0,
               is_active INTEGER DEFAULT 1,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           );
       """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);
    """)

    # === ТАБЛИЦА ИСПОЛЬЗОВАНИЯ ПРОМОКОДОВ (Защита от повторного использования) ===
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS promo_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            promo_code_id INTEGER NOT NULL,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, promo_code_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (promo_code_id) REFERENCES promo_codes(id)
        );
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_promo_usage_user ON promo_usage(user_id);
    """)

    # ✅ ДОБАВЛЯЕМ ЭТУ СТРОКУ ДЛЯ БАРСИКА:
    await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_promo_usage_promo_code ON promo_usage(promo_code_id);
        """)

    await conn.commit()
    logger.info("Migrations completed successfully. Database is clean and ready.")