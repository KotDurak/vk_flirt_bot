import logging
from pathlib import Path
import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """
    Управляет подключением к SQLite и настройкой производительности
    для высокой многопользовательской нагрузки.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        # Создаем папку для базы, если её нет
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        logger.info("Connecting to SQLite database at %s", self.db_path)

        # ВАЖНО для многопользовательской нагрузки:
        # isolation_level="IMMEDIATE" предотвращает deadlock при конкурентной записи
        # check_same_thread=False необходим для корректной работы с asyncio/aiogram
        self._conn = await aiosqlite.connect(
            self.db_path,
            isolation_level="IMMEDIATE",
            check_same_thread=False
        )

        # Возвращаем строки как словари (удобно работать)
        self._conn.row_factory = aiosqlite.Row
        await self._setup_pragmas()

    async def _setup_pragmas(self) -> None:
        """Включаем режимы для максимальной производительности и защиты от блокировок."""
        if not self._conn:
            return

        # WAL позволяет читать и писать одновременно без блокировки всей БД
        await self._conn.execute("PRAGMA journal_mode=WAL;")

        # NORMAL - идеальный баланс скорости и безопасности при использовании WAL
        await self._conn.execute("PRAGMA synchronous=NORMAL;")

        # Ждем 10 секунд, если база занята (защита от пиковых нагрузок)
        await self._conn.execute("PRAGMA busy_timeout=10000;")

        # Включаем внешние ключи (обязательно для каждой новой сессии в SQLite)
        await self._conn.execute("PRAGMA foreign_keys=ON;")

        # Бонус: увеличиваем кэш страниц в памяти до 64 МБ для ускорения записи сообщений
        await self._conn.execute("PRAGMA cache_size=-64000;")

        logger.info("SQLite pragmas configured for high concurrency")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            logger.info("Database connection closed")

    @property
    def connection(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database is not connected! Call .connect() first.")
        return self._conn