from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


@dataclass
class ChatTask:
    """Задача на обработку сообщения."""
    user_id: int
    char_id: int
    peer_id: int
    text: str
    user_dict: dict
    char_dict: dict
    keyboard: str | None = None
    is_regeneration: bool = False
    model_name: str | None = None


class ChatQueue:
    """
    Очередь обработки сообщений с умной защитой от перегрузки LLM API.
    """

    def __init__(
            self,
            max_workers: int = 1,  # Начни с 1, потом повысишь
            retry_delay: float = 2.0,
            max_retries: int = 3,
    ):
        self._queue: asyncio.Queue[ChatTask] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._max_workers = max_workers
        self._retry_delay = retry_delay
        self._max_retries = max_retries
        self._running = False
        self._handler: Callable[[ChatTask], Awaitable[None]] | None = None

    async def start(self, handler: Callable[[ChatTask], Awaitable[None]]):
        """Запускает воркеры."""
        self._handler = handler
        self._running = True
        for i in range(self._max_workers):
            task = asyncio.create_task(self._worker(i))
            self._workers.append(task)
        logger.info("🚀 ChatQueue запущена с %d воркерами", self._max_workers)

    async def add(self, task: ChatTask):
        """Добавляет сообщение в очередь."""
        await self._queue.put(task)
        logger.info(
            "📥 Задача добавлена: user=%s char=%s, размер очереди: %d",
            task.user_id, task.char_id, self._queue.qsize()
        )

    async def _worker(self, worker_id: int):
        """Воркер с умной обработкой ошибок."""
        while self._running:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._process_with_retry(task, worker_id)
            finally:
                self._queue.task_done()

    async def _process_with_retry(self, task: ChatTask, worker_id: int):
        """Обрабатывает задачу с повторными попытками и умной логикой."""
        for attempt in range(self._max_retries):
            try:
                if self._handler:
                    await self._handler(task)
                return  # Успех! Выходим

            except Exception as e:
                error_str = str(e).lower()

                # Определяем тип ошибки
                is_rate_limit = "429" in error_str or "rate limit" in error_str or "too many requests" in error_str
                is_server_error = "500" in error_str or "internal server error" in error_str

                # Для 500 ошибок делаем только 1 retry (бессмысленно долбить упавший сервер)
                max_attempts_for_this_error = 1 if is_server_error else self._max_retries

                if attempt < max_attempts_for_this_error - 1:
                    # Экспоненциальная задержка для 429, фиксированная для 500
                    delay = self._retry_delay * (2 ** attempt) if is_rate_limit else self._retry_delay

                    logger.warning(
                        "⚠️ Воркер %d: Ошибка %s (попытка %d/%d). Пауза %.1f сек. User: %s",
                        worker_id, "429 Rate Limit" if is_rate_limit else "500 Server Error",
                        attempt + 1, max_attempts_for_this_error, delay, task.user_id
                    )
                    await asyncio.sleep(delay)
                else:
                    # Все попытки провалены
                    logger.error(
                        "❌ Воркер %d: Задача провалена. User: %s, Char: %s. Ошибка: %s",
                        worker_id, task.user_id, task.char_id, e
                    )
                    # Здесь можно вызвать функцию, которая напишет юзеру "Извини, нейросеть устала, попробуй через минуту"
                    # await self._notify_user_about_failure(task)
                    return

    async def stop(self):
        """Останавливает воркеры."""
        self._running = False
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        logger.info("🛑 ChatQueue остановлена")

    @property
    def size(self) -> int:
        return self._queue.qsize()