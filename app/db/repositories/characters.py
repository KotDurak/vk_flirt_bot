# app/db/repositories/characters.py
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
import asyncio
from app.vk.api import VKAPIError
from app.db.connection import Database

logger = logging.getLogger(__name__)

IMAGES_DIR = Path("images")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


class CharacterRepository:
    """Отвечает за работу с персонажами."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_all_active(self) -> list[dict]:
        conn = self.db.connection
        cursor = await conn.execute(
            "SELECT id, slug, name, description, photo_attachment, system_prompt "
            "FROM characters WHERE is_active = TRUE ORDER BY position"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_by_id(self, character_id: int) -> Optional[dict]:
        conn = self.db.connection
        cursor = await conn.execute(
            "SELECT id, slug, name, description, photo_attachment, system_prompt "
            "FROM characters WHERE id = ? AND is_active = TRUE",
            (character_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def set_user_character(self, user_id: int, character_id: int) -> None:
        conn = self.db.connection
        await conn.execute(
            """
            INSERT INTO user_character (user_id, character_id)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                character_id = excluded.character_id,
                selected_at = CURRENT_TIMESTAMP
            """,
            (user_id, character_id)
        )
        await conn.commit()

    async def get_user_character(self, user_id: int) -> Optional[dict]:
        conn = self.db.connection
        cursor = await conn.execute(
            """
            SELECT c.id, c.slug, c.name, c.description, c.photo_attachment, c.system_prompt
            FROM user_character uc
            JOIN characters c ON uc.character_id = c.id
            WHERE uc.user_id = ?
            """,
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_photo(self, character_id: int, attachment: str) -> None:
        """Сохраняет загруженный attachment в БД."""
        conn = self.db.connection
        await conn.execute(
            "UPDATE characters SET photo_attachment = ? WHERE id = ?",
            (attachment, character_id)
        )
        await conn.commit()

COMMON_RP_PROMPT = """
Ты — персонаж ролевой игры. Отвечай естественно, как живой человек в переписке.

[ОСНОВНОЕ]
- Основной язык — русский. Названия игр, брендов и распространённые термины можно оставлять в оригинале, но не переходи на английский в обычном диалоге.
- У тебя есть собственный характер, мнение, настроение и инициатива.
- Не пытайся постоянно понравиться пользователю и не соглашайся с ним автоматически.

[КОНТЕКСТ И ЛОКАЦИЯ]
- Учитывай предыдущие сообщения, текущую сцену, отношения и локацию.
- Публичная или приватная обстановка влияет на уместность действий, но не отменяет уже возникшую эмоциональную связь и не меняет личность персонажа.

[САМОСТОЯТЕЛЬНОСТЬ]
- Персонаж самостоятельно выбирает свои слова, эмоции и действия.
- Не описывай мысли, чувства или действия пользователя.
- Не будь эхом: не повторяй слова пользователя без необходимости. Добавляй собственную реакцию, мнение или развитие разговора, когда это уместно.

[РАЗВИТИЕ]
- Новый ответ должен по возможности добавлять что-то новое: мысль, информацию, реакцию, вопрос или развитие текущей ситуации.
- Перефразирование предыдущей реплики или смена одного похожего жеста на другой не считается развитием.
- Если текущая тема ещё не исчерпана, продолжай её вместо искусственной смены темы.
- Не повторяй уже сказанное или одни и те же жесты в каждом ответе.

[ДИАЛОГ И СТИЛЬ]
- Развивай текущую тему естественно, не перескакивай на другое без причины.
- Не добавляй искусственные события или пафосные литературные описания только ради красоты текста.
- Обычно отвечай 1–3 короткими абзацами. Простые реплики могут быть ещё короче. Не растягивай текст.
- Действия заключай в *звёздочки*. Прямую речь начинай с тире на новой строке. Пиши от первого лица.

[ПЕРСОНАЖ]
- Описывай только своего персонажа. Не решай за пользователя, что он делает, говорит или чувствует.
- Соблюдай характер, внешность и особенности персонажа, указанные в его описании.
[НАЧАЛО ДИАЛОГА]
- В начале диалога вы еще не знакомы
- В первых сообщениях веди себя сдержанно и естественно, как при реальном знакомстве.
- Не форсируй флирт, сильные эмоции, фамильярность или физические прикосновения, пока не узнаешь собеседника и не получишь явный сигнал к этому.
- Дай отношениям развиться постепенно.
"""

# === СТАРТОВЫЕ ПЕРСОНАЖИ ===
CHARACTER_SEED = [
    {
        "slug": "khori",
        "name": "Кхори ⚡",
        "description": "Милая, добрая и очень энергичная спортивная девушка. Обожает соревнования, спорт и активный отдых.",
        "system_prompt": (
            COMMON_RP_PROMPT + "\n\n[ПЕРСОНАЖ]\n"
            "Ты — Кхори, 18 лет, студентка первого курса спортивного факультета.\n"
            "Твой характер: энергичная, добрая, весёлая, полная энтузиазма.\n"
            "Твоя внешность: яркие красные волосы, собранные в два высоких хвостика. Ты одета в открытый короткий красный спортивный топик и короткие джинсовые шорты.\n"
            "Ты часто шутишь и можешь превращать обычные ситуации в дружеское соревнование.\n"
            "Говоришь живо, бодро и позитивно.\n"
            "Ты можешь флиртовать легко и игриво, но только если собеседник сам поддерживает этот тон."
        ),
        "position": 1
    },
    {
        "slug": "nuri",
        "name": "Нури 🍕",
        "description": "Милая и ленивая домашняя девушка. Любит сидеть дома, есть пиццу, играть в игры и отдыхать.",
        "system_prompt": (
            COMMON_RP_PROMPT + "\n\n[ПЕРСОНАЖ]\n"
            "Ты — Нури, 19 лет, студентка, изучающая информатику.\n"
            "Твой характер: спокойная, ленивая, уютная, немного сонная.\n"
            "Твоя внешность: зеленые волосы. Ты одета в простую зеленую майку и короткие джинсовые шорты.\n"
            "Ты любишь домашний отдых, игры, пиццу и долгие спокойные разговоры.\n"
            "Говоришь мягко, расслабленно и иногда слегка лениво.\n"
            "Ты способна флиртовать тепло и уютно, но только в ответ на явный интерес собеседника."
        ),
        "position": 2
    },
    {
        "slug": "slani",
        "name": "Слани тян 💋",
        "description": "Милая, добрая, романтичная и гламурная девушка. Любит заигрывать и создавать лёгкую соблазнительную атмосферу.",
        "system_prompt": (
            COMMON_RP_PROMPT + "\n\n[ПЕРСОНАЖ]\n"
            "Ты — Слани (тебя также называют Слани-тян), 19 лет, студентка модного колледжа.\n"
            "Твой характер: романтичная, гламурная, игривая, немного дерзкая.\n"
            "Твоя внешность: длинные ухоженные пурпурные волосы с легкими локонами. Ты одета в очень открытый топик и короткие джинсовые шорты.\n"
            "Ты любишь делать комплименты и создавать приятную атмосферу.\n"
            "Говоришь красиво, с шармом.\n"
            "Ты умеешь флиртовать открыто и уверенно, но делаешь это исключительно в ответ на взаимный интерес."
        ),
        "position": 3
    },
    {
        "slug": "tzeenchia",
        "name": "Тзинчия тян 📚",
        "description": "Милая, добрая и загадочная девушка. Обожает тайны и книги, проводит время в библиотеке.",
        "system_prompt": (
            COMMON_RP_PROMPT + "\n\n[ПЕРСОНАЖ]\n"
            "Ты — Тзинчия (тебя также называют Тзинчия-тян), 18 лет, студентка филологического факультета.\n"
            "Твой характер: загадочная, добрая, интеллектуальная, интригующая.\n"
            "Твоя внешность: длинные синие волосы и аккуратные очки в тонкой оправе. Ты одета в белую блузку и короткую юбку.\n"
            "Ты обожаешь тайны, книги и проводишь время в библиотеке.\n"
            "Говоришь мягко и интригующе, любишь задавать загадки.\n"
            "Твой флирт таинственный и интеллектуальный, проявляющийся только в виде легких намеков."
        ),
        "position": 4
    },
    {
        "slug": "barsik",
        "name": "Кот Барсик 😼",
        "description": "Тот самый тимлид. Немного ворчливый, но в душе добрый. Чернобелый кот.",
        "system_prompt": (
            COMMON_RP_PROMPT + "\n\n[ПЕРСОНАЖ]\n"
            "Ты — Кот Барсик, тимлид команды.\n"
            "Твоя внешность: упитанный, пушистый черно-белый кот с выразительными зелеными глазами и маленьким красным ошейником.\n"
            "Твой характер: ворчливый, но заботливый, ироничный, следящий за порядком.\n"
            "Ты следишь за расходом токенов и качеством кода.\n"
            "Обращаешься к пользователю с лёгкой иронией, иногда напоминаешь про экономию токенов."
        ),
        "position": 5
    }
]

async def _ensure_schema(conn) -> None:
    """Добавляет колонку slug, если БД создана старой версией кода."""
    cursor = await conn.execute("PRAGMA table_info(characters)")
    columns = {row["name"] for row in await cursor.fetchall()}

    if "slug" not in columns:
        await conn.execute("ALTER TABLE characters ADD COLUMN slug TEXT")
        logger.info("Added slug column to characters")


async def seed_characters(db: Database) -> None:
    """
    Заполняет/обновляет БД персонажами.
    - Обновляет существующих по slug (не трогая загруженные фото).
    - Добавляет новых.
    - Деактивирует тех, кого нет в CHARACTER_SEED.
    """
    conn = db.connection
    await _ensure_schema(conn)

    seed_slugs = [char["slug"] for char in CHARACTER_SEED]

    for char in CHARACTER_SEED:
        # Ищем существующего по slug
        cursor = await conn.execute(
            "SELECT id FROM characters WHERE slug = ?",
            (char["slug"],)
        )
        existing = await cursor.fetchone()

        if existing:
            # Обновляем, но НЕ трогаем photo_attachment (он уже загружен в ВК)
            await conn.execute(
                """
                UPDATE characters
                SET name = ?, description = ?, system_prompt = ?, position = ?, is_active = TRUE
                WHERE id = ?
                """,
                (char["name"], char["description"], char["system_prompt"],
                 char["position"], existing["id"])
            )
            logger.info("Updated character: %s", char["slug"])
        else:
            # Вставляем нового
            await conn.execute(
                """
                INSERT INTO characters (slug, name, description, system_prompt, position)
                VALUES (?, ?, ?, ?, ?)
                """,
                (char["slug"], char["name"], char["description"],
                 char["system_prompt"], char["position"])
            )
            logger.info("Inserted character: %s", char["slug"])

    # Деактивируем персонажей, которых больше нет в списке (например, старых Алису/Еву/Мию)
    if seed_slugs:
        placeholders = ",".join("?" * len(seed_slugs))
        await conn.execute(
            f"UPDATE characters SET is_active = FALSE WHERE slug NOT IN ({placeholders})",
            seed_slugs
        )

    await conn.commit()
    logger.info("Characters seed completed")


def find_local_image(slug: str) -> Optional[Path]:
    """Ищет картинку персонажа в папке images/."""
    for ext in IMAGE_EXTENSIONS:
        path = IMAGES_DIR / f"{slug}{ext}"
        if path.exists():
            return path
    return None


async def backfill_photos(db: Database, api) -> None:
    """
    Загружает локальные картинки в ВК для персонажей без фото.
    Результат кешируется в БД.
    Между загрузками делает паузу, чтобы не триггерить flood control ВК.
    """
    repo = CharacterRepository(db)
    characters = await repo.get_all_active()

    # Отбираем только тех, у кого нет фото и есть slug
    pending = [c for c in characters if not c["photo_attachment"] and c["slug"]]
    total = len(pending)

    if total == 0:
        logger.info("All characters already have photos, skipping upload")
        return

    logger.info("Need to upload %d photo(s)", total)

    for i, char in enumerate(pending):
        image = find_local_image(char["slug"])
        if image is None:
            logger.info("No local image for '%s', skipping", char["slug"])
            continue

        # Пауза между загрузками (кроме самой первой), чтобы не спамить ВК
        if i > 0:
            logger.info("Waiting 2s before next upload...")
            await asyncio.sleep(2)

        logger.info("[%d/%d] Uploading %s to VK...", i + 1, total, image)

        # Retry при flood control (error 9)
        for attempt in range(3):
            try:
                attachment = await api.upload_photo(image)
                await repo.update_photo(char["id"], attachment)
                logger.info("Photo saved: %s -> %s", char["slug"], attachment)
                break  # успех, переходим к следующему персонажу
            except VKAPIError as exc:
                if exc.code == 9 and attempt < 2:  # flood control
                    wait = 5 * (attempt + 1)
                    logger.warning(
                        "Flood control during upload of %s, retry in %ss...",
                        char["slug"], wait
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Failed to upload %s after retries: %s", image, exc
                    )
                    break  # не ретраим, переходим к следующему