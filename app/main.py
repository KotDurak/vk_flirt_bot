import asyncio
import logging
import ssl
from functools import partial

import aiohttp
import certifi
import time
from app.config import get_settings
from app.logging_setup import setup_logging
from app.services.chat_queue import ChatQueue, ChatTask
from app.services.chat_worker import process_chat_task   # <-- НОВОЕ
from app.handlers.messages import handle_update
from app.vk.api import VKApi
from app.vk.longpoll import LongPollClient
from app.db.connection import Database
from app.db.migrations import run_migrations
from app.db.repositories.users import UserRepository
from app.db.repositories.characters import CharacterRepository, seed_characters, backfill_photos
from app.db.repositories.messages import MessageRepository
from app.db.repositories.summaries import SummaryRepository
from app.services.event_cache import EventCache

logger = logging.getLogger(__name__)


async def run_bot() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    # === 1. База данных ===
    db = Database(settings.db_path)
    await db.connect()
    await run_migrations(db)
    await seed_characters(db)

    user_repo = UserRepository(db)
    char_repo = CharacterRepository(db)
    msg_repo = MessageRepository(db)
    summary_repo = SummaryRepository(db)

    # === 2. API-клиент ===
    timeout = aiohttp.ClientTimeout(total=settings.longpoll_wait + 30)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        api = VKApi(
            session=session,
            token=settings.group_token,
            api_version=settings.api_version,
        )

        # === 3. Загружаем картинки ===
        await backfill_photos(db, api)

        # === 4. 🆕 Очередь чата ===
        # Один воркер + пауза 5 секунды между запросами к API.
        # Когда узнаешь точный лимит Polza.ai — подстрой delay_between_requests.
        chat_queue = ChatQueue(
            max_workers=1,
            delay_between_requests=5.0,
        )

        # Фиксируем зависимости в обработчик через partial
        task_handler = partial(
            process_chat_task,
            api=api,
            session=session,
            msg_repo=msg_repo,
            summary_repo=summary_repo,
        )

        # Запускаем воркеры
        await chat_queue.start(task_handler)
        asyncio.create_task(queue_monitor(chat_queue))

        longpoll = LongPollClient(
            api=api, group_id=settings.group_id, wait=settings.longpoll_wait
        )

        logger.info("Starting VK bot with chat queue")
        event_cache = EventCache()

        try:
            async for update in longpoll.updates():
                try:
                    await handle_update(
                        update=update,
                        api=api,
                        group_id=settings.group_id,
                        session=session,
                        user_repo=user_repo,
                        char_repo=char_repo,
                        msg_repo=msg_repo,
                        summary_repo=summary_repo,
                        event_cache=event_cache,
                        chat_queue=chat_queue,      # <-- НОВОЕ
                    )
                except Exception:
                    logger.exception("Failed to handle update")
        finally:
            # 🆕 Останавливаем очередь ПЕРЕД закрытием БД
            await chat_queue.stop()
            await db.close()


def main() -> None:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("Bot stopped")


async def queue_monitor(chat_queue: ChatQueue):
    """Логирует размер очереди каждые 30 секунд."""
    while True:
        await asyncio.sleep(30)
        logger.info("📊 Queue size: %d tasks", chat_queue.size)

if __name__ == "__main__":
    main()