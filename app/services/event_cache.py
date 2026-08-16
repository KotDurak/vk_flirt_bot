from __future__ import annotations
import time
from collections import OrderedDict


class EventCache:
    """Хранит ID последних обработанных событий от ВК."""

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 60):
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        now = time.time()

        # Чистим старые записи
        while self._cache and (now - next(iter(self._cache.values())) > self._ttl):
            self._cache.popitem(last=False)

        if event_id in self._cache:
            return True

        self._cache[event_id] = now
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
        return False