from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


@dataclass
class ChatTask:
    """Задача на обработку сообщения."""
    user_id: int
    char_id: int
    peer_id: int
    text: str
    user_dict: dict  # чтобы не искать заново
    char_dict: dict  # чтобы не искать заново
    # Приоритет: 0 = обычный, 1 = премиум (обрабатывается первым)
    priority: int = 0
    keyboard: str | None = None
    is_regeneration: bool = False
    model_name: str = None

class ChatQueue:
    """
    Очередь обработки сообщений с контролируемой нагрузкой на LLM API.

    Преимущества:
    - Максимум N одновременных запросов к LLM
    - Пауза между запросами (защита от 429)
    - Пользователи не блокируют друг друга
    """

    def __init__(
            self,
            max_workers: int = 1,
            delay_between_requests: float = 2.0,
    ):
        self._queue: asyncio.Queue[ChatTask] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._max_workers = max_workers
        self._delay = delay_between_requests
        self._running = False
        self._handler: Callable[[ChatTask], Awaitable[None]] | None = None

    async def start(self, handler: Callable[[ChatTask], Awaitable[None]]):
        """Запускает воркеры. Вызывается при старте приложения."""
        self._handler = handler
        self._running = True
        for i in range(self._max_workers):
            task = asyncio.create_task(self._worker(i))
            self._workers.append(task)
        logger.info("🚀 ChatQueue started with %d workers", self._max_workers)

    async def add(self, task: ChatTask):
        """Добавляет сообщение в очередь."""
        await self._queue.put(task)
        logger.info(
            "📥 Task queued: user=%s char=%s queue_size=%d",
            task.user_id, task.char_id, self._queue.qsize()
        )

    async def _worker(self, worker_id: int):
        """Воркер: берёт задачи из очереди и обрабатывает по одной."""
        while self._running:
            try:
                # Ждём задачу максимум 1 секунду (чтобы не зависнуть при остановке)
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                if self._handler:
                    await self._handler(task)
            except Exception:
                logger.exception("Worker %d failed to process task", worker_id)
            finally:
                self._queue.task_done()
                # ⚡ КЛЮЧЕВОЕ: пауза между запросами к API
                await asyncio.sleep(self._delay)

    async def stop(self):
        """Останавливает воркеры. Вызывается при выключении."""
        self._running = False
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        logger.info("ChatQueue stopped")

    @property
    def size(self) -> int:
        return self._queue.qsize()