from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .api import VKApi

logger = logging.getLogger(__name__)


class LongPollClient:
    """
    Клиент Bots Long Poll API ВКонтакте.

    Получает события сообщества в реальном времени.
    """

    def __init__(
        self,
        api: VKApi,
        group_id: int,
        wait: int = 25,
    ) -> None:
        self._api = api
        self._group_id = group_id
        self._wait = wait

    async def _get_server(self) -> tuple[str, str, int]:
        """
        Получает адрес Long Poll сервера, ключ и ts.
        """
        response = await self._api.call(
            "groups.getLongPollServer",
            group_id=self._group_id,
        )

        if not response or not all(
            key in response for key in ("server", "key", "ts")
        ):
            raise RuntimeError(
                f"Invalid Long Poll server response: {response}"
            )

        server = str(response["server"])
        key = str(response["key"])
        ts = int(response["ts"])

        return server, key, ts

    async def _safe_get_server(self) -> tuple[str, str, int]:
        """
        Несколько раз пытается получить Long Poll сервер.

        Это нужно, чтобы не падать из-за разовых сетевых ошибок.
        """
        for attempt in range(1, 6):
            try:
                return await self._get_server()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                wait_seconds = min(2 ** attempt, 30)
                logger.warning(
                    "Failed to get Long Poll server, attempt %s: %s; retry in %ss",
                    attempt,
                    exc,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)

        raise RuntimeError("Failed to get Long Poll server after 5 attempts")

    async def updates(self) -> AsyncIterator[dict[str, Any]]:
        """
        Генератор событий Long Poll.

        Использование:
            async for update in longpoll.updates():
                ...
        """
        server, key, ts = await self._safe_get_server()
        logger.info("Long Poll connected")

        while True:
            try:
                params = {
                    "act": "a_check",
                    "key": key,
                    "ts": ts,
                    "wait": self._wait,
                }

                async with self._api.session.get(
                    server,
                    params=params,
                ) as response:
                    body = await response.json(content_type=None)

                if not isinstance(body, dict):
                    raise ValueError(f"Unexpected Long Poll body: {body!r}")

                failed = body.get("failed")
                if failed:
                    logger.warning(
                        "Long Poll returned failed=%s; reconnecting",
                        failed,
                    )
                    server, key, ts = await self._safe_get_server()
                    continue

                ts = int(body.get("ts", ts))

                updates = body.get("updates") or []
                if not isinstance(updates, list):
                    raise ValueError(
                        f"Unexpected Long Poll updates field: {updates!r}"
                    )

                for update in updates:
                    yield update

            except asyncio.CancelledError:
                logger.info("Long Poll stopped")
                raise

            except aiohttp.ClientError as exc:
                logger.warning(
                    "Long Poll network error: %s; reconnecting",
                    exc,
                )
                await asyncio.sleep(1)
                server, key, ts = await self._safe_get_server()

            except Exception:
                logger.exception("Unexpected Long Poll error; reconnecting")
                await asyncio.sleep(3)
                server, key, ts = await self._safe_get_server()