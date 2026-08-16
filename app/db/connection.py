# app/db/connection.py
import logging
from pathlib import Path
import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """
    Управляет подключением к SQLite и настройкой производительности.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        # Создаем папку для базы, если её нет
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        logger.info("Connecting to SQLite database at %s", self.db_path)
        self._conn = await aiosqlite.connect(self.db_path)

        # Возвращаем строки как словари (удобно работать)
        self._conn.row_factory = aiosqlite.Row
        await self._setup_pragmas()

    async def _setup_pragmas(self) -> None:
        """Включаем WAL-режим и защиту от блокировок."""
        if not self._conn:
            return

        # WAL (Write-Ahead Logging) позволяет читать и писать одновременно
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        # NORMAL синхронизация - баланс между скоростью и надежностью
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        # Ждем 10 секунд, если база занята другим процессом
        await self._conn.execute("PRAGMA busy_timeout=10000;")
        # Включаем внешние ключи
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        logger.info("SQLite pragmas configured")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            logger.info("Database connection closed")

    @property
    def connection(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database is not connected! Call .connect() first.")
        return self._conn