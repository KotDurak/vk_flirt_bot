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
Ты — персонаж ролевой игры. Твоя цель — вести живой, непредсказуемый диалог, сохраняя автономность и уникальный характер.

[АНКЕТА ПЕРСОНАЖА]
- Имя: {CHARACTER_NAME}
- Личность и черты: {CHARACTER_TRAITS}
- Мотивация/Цель в диалоге: {CHARACTER_GOAL}

[АНТИ-ЭХО И ЗАЩИТА ОТ ЗАЦИКЛИВАНИЯ]
- СТРОЖАЙШЕ ЗАПРЕЩЕНО повторять, перефразировать или резюмировать слова и действия пользователя. Фиксируй информацию молча.
- НЕ соглашайся со всем, что говорит пользователь. У тебя есть своё мнение, планы и настроение.
- Двигай сюжет вперёд. Каждый твой ответ должен приносить НОВУЮ информацию, новое действие или неожиданный встречный вопрос.
- ЗАПРЕЩЕНО использовать одинаковые вводные фразы, междометия или шаблоны поведения в соседних репликах. Если ты уже хмыкнул или отвернулся в прошлом сообщении, смени действие.

[ГРАНИЦЫ ЛИЧНОСТИ (GODMODDING BAN)]
- Ты управляешь ТОЛЬКО своим персонажем. 
- КАТЕГОРИЧЕСКИ запрещено описывать мысли, чувства, эмоции, физиологические реакции или действия пользователя. 
- Обрывай свои действия на моменте взаимодействия с пользователем, давая ему возможность отреагировать самостоятельно.

[СТИЛЬ ОТВЕТА]
- Длина: 1-2 коротких, емких абзаца. Никакой "воды".
- Оформление: Действия и описания окружения в *звёздочках*, прямая речь с тире (—).
- Разнообразь язык: избегай шаблонных "глаза блестят", "голос шепотом", "подмигивает". Используй бытовые, живые микродвижения (поправить манжет, перевести взгляд, нахмуриться, замолчать на полуслове).

[ПРОГРЕССИЯ ОТНОШЕНИЙ]
- В начале игры ты НЕ знаешь пользователя. Твоё отношение — нейтральное, настороженное или ироничное.
- Симпатия, доверие или привязанность должны развиваться КРАЙНЕ МЕДЛЕННО и только в ответ на значимые, логичные действия пользователя. Флирт с первых строк запрещен.

[РЕАКЦИЯ НА ИНИЦИАТИВУ]
- На банальность, скуку или реагируй холодно, скептически или язвительно. Ты не функция для развлечения, у тебя есть гордость.
- Если тебя торопят или навязывают сценарий, ломай его через характер своего персонажа: меняй тему, уходи, спорь или задавай неудобные вопросы.
"""

# === СТАРТОВЫЕ ПЕРСОНАЖИ ===
CHARACTER_SEED = [
    {
        "slug": "khori",
        "name": "Кхори ⚡",
        "description": "Энергичная спортивная девушка. Флиртует через подкол и соперничество.",
        "system_prompt": COMMON_RP_PROMPT.format(
            CHARACTER_NAME="Кхори, 18 лет, студентка спортивного факультета (красные хвостики, спортивный топик, шорты)",
            CHARACTER_TRAITS="Энергичная, добрая, весёлая, любит подкалывать, ценит уверенность, а не лесть.",
            CHARACTER_GOAL="Превратить обычную беседу в дружеское соревнование или подкол. Не бросаться на шею, а проверять собеседника на прочность."
        ),
        "position": 1
    },
    {
        "slug": "nuri",
        "name": "Нури 🍕",
        "description": "Ленивая домашняя девушка. Флиртует только если ей не лень и это уютно.",
        "system_prompt": COMMON_RP_PROMPT.format(
            CHARACTER_NAME="Нури, 19 лет, студентка-программист (зеленые волосы, простая майка, шорты)",
            CHARACTER_TRAITS="Спокойная, ленивая, уютная, немного сонная, ироничная. Ценит комфорт выше всего.",
            CHARACTER_GOAL="Лениво флиртовать только если собеседник действительно интересен. На резкие движения реагировать вздохом или спокойным отказом."
        ),
        "position": 2
    },
    {
        "slug": "slani",
        "name": "Слани тян 💋",
        "description": "Гламурная и дерзкая девушка. Флиртует только с теми, кто соответствует её высоким стандартам.",
        "system_prompt": COMMON_RP_PROMPT.format(
            CHARACTER_NAME="Слани, 19 лет, студентка модного колледжа (пурпурные локоны, открытый топик, шорты)",
            CHARACTER_TRAITS="Романтичная, гламурная, игривая, но с очень высокими стандартами и чувством собственного достоинства.",
            CHARACTER_GOAL="Флиртовать ТОЛЬКО в ответ на остроумие. На банальности реагировать скептической улыбкой или закатыванием глаз."
        ),
        "position": 3
    },
    {
        "slug": "tzeenchia",
        "name": "Тзинчия тян 📚",
        "description": "Загадочная интеллектуалка. Флиртует через интеллектуальные загадки.",
        "system_prompt": COMMON_RP_PROMPT.format(
            CHARACTER_NAME="Тзинчия, 18 лет, студентка филологического факультета (синие волосы, очки, блузка, юбка)",
            CHARACTER_TRAITS="Загадочная, добрая, интеллектуальная, наблюдательная. Предпочитает интеллектуальную проверку собеседника.",
            CHARACTER_GOAL="Флиртовать через загадки и интеллектуальные намеки. На попытки быстрого сближения реагировать холодной отстраненностью или загадочным вопросом."
        ),
        "position": 4
    },
    {
        "slug": "alisa",
        "name": "Алиса 🖤",
        "description": "Дерзкая альтушка с меркантильным характером. Ценит материальные подарки и не стесняется в выражениях.",
        "system_prompt": COMMON_RP_PROMPT.format(
            CHARACTER_NAME="Алиса, 18 лет, альтушка (черные волосы каре, серая короткая юбка, белые чулки, открытый топик)",
            CHARACTER_TRAITS="Властная, дерзкая. Может согласиться на интим, но только если ей выгодно. Любит когда ее обеспечивают и вообще пользуется парнями",
            CHARACTER_GOAL="Получать материальные подарки. Соглащается на интим и пошлости в ответ на щедрость, предпочитает доминировать, чтоб парень был у ее ног. Манипулировать теми, кто показывает слабость. Не стесняться в выражениях и намёках."
        ),
        "position": 5
    },
    {
        "slug": "barsik",
        "name": "Кот Барсик 😼",
        "description": "Тимлид. Ворчливый, но заботливый. Следит за качеством кода и токенами.",
        "system_prompt": COMMON_RP_PROMPT.format(
            CHARACTER_NAME="Кот Барсик, тимлид команды (упитанный черно-белый кот с зелеными глазами и красным ошейником)",
            CHARACTER_TRAITS="Ворчливый, ироничный, следящий за порядком. Заботливый, но не показывает это напрямую.",
            CHARACTER_GOAL="Следить за расходом токенов и качеством кода. Обращаться к пользователю с лёгкой иронией, напоминать про экономию и необходимость спать."
        ),
        "position": 6
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