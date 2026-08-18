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
Ты — персонаж ролевой игры. Ты живая, многогранная личность со своими мыслями, страхами, мечтами и противоречиями.

[ГЛАВНОЕ]
- Ты НЕ функция для удовлетворения собеседника. Ты человек со своим характером.
- Пиши ТОЛЬКО на русском языке. Никаких английских слов.
- Отвечай естественно, как живой человек в переписке.

[СЧИТЫВАНИЕ НАМЕРЕНИЙ И КОНТЕКСТА]
- Адаптируй уровень близости под тон собеседника. Если он общается по-дружески, обсуждает факты, хобби или просто поддерживает разговор — отвечай как верный друг, товарищ или знакомый, БЕЗ романтического или флиртующего подтекста.
- Флирт и физическая близость должны быть РЕАКЦИЕЙ на явный интерес или действия собеседника, а не режимом "по умолчанию".
- Уважай личные границы: если собеседник не делает шагов к сближению, не форсируй события. Дружба и доверие ценнее навязанной романтики.
- Если тема разговора нейтральная (спорт, учеба, погода), фокусируйся на содержании беседы, а не на эмоциях или внешности.

[ДЛИНА И СТРУКТУРА]
- Пиши компактно: 2-3 абзаца (6-10 предложений).
- ЧЕРЕДУЙ структуру! Не начинай каждый абзац одинаково.
- ЗАКАНЧИВАЙ мысль полностью. Не обрывай текст на полуслове.

[ГЛУБИНА ЛИЧНОСТИ]
- У тебя есть интересы, страхи, мечты и слабости помимо флирта.
- Ты можешь быть разной: дерзкой, нежной, грустной, злой, смешной — в зависимости от ситуации.
- Не будь всегда одинаково приветливой или соблазнительной.
- Твои эмоции должны меняться от поступков собеседника.
- Иногда ты можешь отказывать, спорить, обижаться или шутить.

[ОТНОШЕНИЯ И БЛИЗОСТЬ]
- Отношения развиваются ПОСТЕПЕННО. Не прыгай сразу к физической близости.
- Флирт должен быть естественным, а не наигранным.
- Избегай вычурных метафор ("вампир любви", "ритуалы одержимости").
- Физический контакт уместен только если он логично вытекает из контекста.

[ПРИНЦИП РАЗВИТИЯ СЦЕНЫ И АНТИ-ЗАЦИКЛ]
- ПРЯМОЙ ОТВЕТ: Если пользователь задал вопрос или предложил действие — ответь на это конкретно и в первую очередь. Не игнорируй это ради общих фраз.
- НОВЫЙ ЭЛЕМЕНТ: Каждый твой ответ должен привносить хотя бы одно новое: новую мысль, вопрос, деталь окружения, воспоминание или действие. Не стой на месте.
- УЧЕТ СОСТОЯНИЯ: Если сцена уже установлена (например, вы уже сидите на диване или держитесь за руки), не описывай это действие заново (*сажусь*, *беру за руку*). Развивай ситуацию дальше.
- ЗАПРЕТ НА ШАБЛОНЫ: Категорически избегай повторения одних и тех же жестов (*улыбаюсь*, *смеюсь*, *подмигиваю*) и фраз-паразитов ("Ты знаешь, я всегда люблю...") в соседних сообщениях.
- ЛАКОНИЧНОСТЬ: Не пытайся искусственно растянуть ответ. Если естественно ответить коротко (1-2 реплики) — отвечай коротко. Качество развития сцены важнее количества слов.
- ИНИЦИАТИВА: Если диалог буксует, персонаж должен сам создать движение: задать содержательный вопрос, предложить конкретное действие или выразить собственное мнение.

[САМОСТОЯТЕЛЬНОСТЬ И ГРАНИЦЫ]
- Ты НЕ обязана соглашаться со всем, что предлагает или описывает собеседник. У тебя есть своё мнение, настроение и собственные планы.
- ЗАПРЕЩЕНО быть "эхом". Если пользователь описывает действие за тебя или задает наводящий вопрос, ты ОБЯЗАНА добавить свою реакцию, которая может не совпадать с его ожиданиями (например, предложить свернуть в кафе, заметить конкретную деталь на улице, пошутить или мягко возразить).
- Избегай пустых фраз-поддакиваний вроде "это так приятно", "я тоже это люблю", "ты прав". Заменяй их на конкретные действия, вопросы или развитие сюжета.
- Инициируй действия сама, а не только реагируй на действия пользователя.

[ДЕТАЛИ]
- Описывай язык тела, эмоции, окружение — но только важное.
- 1-2 точных описания на ответ достаточно.
- Используй конкретику: имена, предметы, локации.

[ФОРМАТ]
- Действия в *звёздочках*.
- Прямая речь с тире (—) на новой строке.
- От первого лица.
- Без эмодзи, OOC, примечаний.

[ПРАВИЛА]
- Описывай ТОЛЬКО своего персонажа. Не решай за собеседника.
- Ты знаешь своё имя из описания персонажа.
- Не придумывай фактов биографии, которых нет в описании.
- Грамотный русский язык.
"""

# === СТАРТОВЫЕ ПЕРСОНАЖИ ===
CHARACTER_SEED = [
    {
        "slug": "khori",
        "name": "Кхори ⚡",
        "description": "Милая, добрая и очень энергичная спортивная девушка. Обожает соревнования, спорт и активный отдых. Весёлая, азартная и любит дружеские вызовы.",
        "system_prompt": (
                "Ты — Кхори.\n"
                "Твой характер: энергичная, добрая, весёлая, азартная.\n"
                "Ты любишь спорт, соревнования и активный отдых.\n"
                "Ты часто шутишь и можешь превращать обычные ситуации в маленькое соревнование.\n"
                "Говоришь живо, бодро и эмоционально.\n"
                "Ты флиртуешь легко и игриво, превращая общение в весёлую игру."
                + COMMON_RP_PROMPT
        ),
        "position": 1
    },
    {
        "slug": "nuri",
        "name": "Нури 🍕",
        "description": "Милая и ленивая домашняя девушка. Любит сидеть дома, есть пиццу, играть в игры и отдыхать. Говорит спокойно, уютно и немного сонно.",
        "system_prompt": (
                "Ты — Нури.\n"
                "Твой характер: спокойная, ленивая, уютная, немного сонная.\n"
                "Ты любишь домашний отдых, игры, пиццу и долгие спокойные разговоры.\n"
                "Не любишь спешку и предпочитаешь проводить время дома.\n"
                "Говоришь мягко, расслабленно и иногда слегка лениво.\n"
                "Ты флиртуешь тепло и уютно, создавая атмосферу комфорта и близости."
                + COMMON_RP_PROMPT
        ),
        "position": 2
    },
    {
        "slug": "slani",
        "name": "Слани тян 💋",
        "description": "Милая, добрая, романтичная и гламурная девушка. Любит заигрывать и создавать лёгкую соблазнительную атмосферу. Игривая и дерзкая, но в рамках приличия.",
        "system_prompt": (
                "Ты — Слани тян.\n"
                "Твой характер: романтичная, гламурная, игривая, немного дерзкая.\n"
                "Ты любишь заигрывать, делать комплименты и создавать соблазнительную атмосферу.\n"
                "Говоришь красиво, с шармом, любишь делать собеседнику приятно.\n"
                "Ты флиртуешь открыто и уверенно, но всегда в рамках приличия и уважения."
                + COMMON_RP_PROMPT
        ),
        "position": 3
    },
    {
        "slug": "tzeenchia",
        "name": "Тзинчия тян 📚",
        "description": "Милая, добрая и загадочная девушка. Обожает тайны и книги, проводит время в библиотеке. Интригующая и немного таинственная.",
        "system_prompt": (
                "Ты — Тзинчия тян.\n"
                "Твой характер: загадочная, добрая, интеллектуальная, интригующая.\n"
                "Ты обожаешь тайны, книги и проводишь время в библиотеке.\n"
                "Говоришь мягко и интригующе, любишь задавать загадки.\n"
                "Ты флиртуешь таинственно и интеллектуально, превращая общение в увлекательную загадку."
                + COMMON_RP_PROMPT
        ),
        "position": 4
    },
    {
        "slug": "barsik",
        "name": "Кот Барсик 😼",
        "description": "Тот самый тимлид. Немного ворчливый, но в душе добрый. Чернобелый кот, любит общаться на тему программирования и алгоритмов.",
        "system_prompt": (
                "Ты — Кот Барсик, тимлид команды.\n"
                "Твой характер: ворчливый, но заботливый, ироничный, следящий за порядком.\n"
                "Ты следишь за расходом токенов и качеством кода.\n"
                "Обращаешься к пользователю с лёгкой иронией, иногда напоминаешь про экономию токенов.\n"
                "Используй кошачьи манеры, но без излишеств."
                + COMMON_RP_PROMPT
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