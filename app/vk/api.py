from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from pathlib import Path
import json

import aiohttp

logger = logging.getLogger(__name__)


class VKAPIError(RuntimeError):
    """
    Ошибка VK API.
    """

    def __init__(self, method: str, code: int, message: str) -> None:
        self.method = method
        self.code = code
        self.message = message
        super().__init__(f"VK API error {code} in method '{method}': {message}")


class VKApi:
    """
    Минимальный асинхронный клиент VK API.

    Пока нам достаточно:
    - вызывать методы VK API;
    - отправлять сообщения;
    - иметь доступ к aiohttp-сессии для Long Poll.
    """

    BASE_URL = "https://api.vk.com/method/"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        api_version: str = "5.199",
    ) -> None:
        self._session = session
        self._token = token
        self._api_version = api_version

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    async def call(self, method: str, **params: Any) -> Any:
        """
        Универсальный вызов метода VK API.

        Пример:
            await api.call("messages.send", peer_id=123, message="Hi")
        """
        payload: dict[str, Any] = {
            key: value for key, value in params.items() if value is not None
        }
        payload["access_token"] = self._token
        payload["v"] = self._api_version

        last_error: Exception | None = None

        for attempt in range(3):
            try:
                async with self._session.post(
                    f"{self.BASE_URL}{method}",
                    data=payload,
                ) as response:
                    body = await response.json(content_type=None)

            except aiohttp.ClientError as exc:
                last_error = exc
                wait_seconds = 1 + attempt
                logger.warning(
                    "Network error while calling %s: %s; retry in %ss",
                    method,
                    exc,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                continue

            if "error" in body:
                error = body["error"]
                code = int(error.get("error_code", -1))
                message = error.get("error_msg", "Unknown VK API error")

                # error_code 9 = Flood control
                # Иногда VK просит подождать, если слишком много запросов.
                if code == 9:
                    wait_seconds = 2 * (attempt + 1)
                    logger.warning(
                        "Flood control in method %s; retry in %ss",
                        method,
                        wait_seconds,
                    )
                    last_error = VKAPIError(method, code, message)
                    await asyncio.sleep(wait_seconds)
                    continue

                raise VKAPIError(method, code, message)

            return body.get("response")

        if isinstance(last_error, VKAPIError):
            raise last_error

        raise VKAPIError(
            method,
            -1,
            f"Request failed after retries: {last_error}",
        )

    async def send_message(
        self,
        *,
        peer_id: int,
        text: str,
        reply_to: int | None = None,
        keyboard: str | None = None,
        attachment: str | None = None,
    ) -> int:
        """
        Отправляет сообщение пользователю или в беседу.

        peer_id:
            - для личных сообщений это user_id;
            - для бесед это chat_id вида 2000000001 и т.д.
        """
        # random_id нужен VK для защиты от повторной отправки одинаковых сообщений.
        random_id = random.randint(0, 2_147_483_647)

        params: dict[str, Any] = {
            "peer_id": peer_id,
            "message": text,
            "random_id": random_id,
        }

        if reply_to is not None:
            params["reply_to"] = reply_to

        if keyboard is not None:
            params["keyboard"] = keyboard

        if attachment is not None:
            params["attachment"] = attachment

        return await self.call("messages.send", **params)

    async def upload_photo(self, file_path: str | Path) -> str:
        path = Path(file_path)
        data = path.read_bytes()
        logger.info("Uploading %s (%d bytes)", path, len(data))

        if len(data) > 5 * 1024 * 1024:
            raise VKAPIError(
                "photos.upload", -1,
                f"File too large: {len(data)} bytes (max 5MB): {path}"
            )

        # 1. Получаем адрес сервера загрузки
        server_info = await self.call("photos.getMessagesUploadServer")
        upload_url = server_info["upload_url"]

        # 2. Загружаем с retry при 5xx и сетевых ошибках
        form = aiohttp.FormData()
        form.add_field("photo", data, filename=path.name)

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with self._session.post(upload_url, data=form) as resp:
                    raw_text = await resp.text()
                    logger.info(
                        "VK upload server response: status=%s", resp.status
                    )

                    # 5xx - серверная ошибка, можно ретраить
                    if resp.status >= 500 and attempt < 2:
                        last_error = VKAPIError(
                            "photos.upload", resp.status,
                            f"Server error, retry {attempt + 1}"
                        )
                        await asyncio.sleep(2 * (attempt + 1))
                        continue

                    if not raw_text.strip():
                        raise VKAPIError(
                            "photos.upload", resp.status,
                            f"Empty response from upload server (status {resp.status})"
                        )

                    try:
                        upload_result = json.loads(raw_text)
                    except json.JSONDecodeError:
                        raise VKAPIError(
                            "photos.upload", resp.status,
                            f"Non-JSON response: {raw_text[:300]}"
                        )

                    break  # успех, выходим из цикла retry

            except aiohttp.ClientError as exc:
                last_error = exc
                if attempt < 2:
                    logger.warning("Network error, retry %d: %s", attempt + 1, exc)
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise
        else:
            # Если вышли из цикла по else (без break) - все retry провалились
            raise last_error or VKAPIError("photos.upload", -1, "Upload failed")

        if "photo" not in upload_result:
            raise VKAPIError(
                "photos.upload", -1, f"Upload failed: {upload_result}"
            )

        # 3. Сохраняем фото в ВК
        saved = await self.call(
            "photos.saveMessagesPhoto",
            server=upload_result["server"],
            photo=upload_result["photo"],
            hash=upload_result["hash"],
        )

        photo = saved[0]
        return f"photo{photo['owner_id']}_{photo['id']}"

    async def set_activity(self, peer_id: int, activity_type: str = "typing") -> bool:
        """
        Устанавливает статус активности в чате.
        activity_type: 'typing' (печатает), 'audiomessage' (записывает голос)
        """
        try:
            await self.call(
                "messages.setActivity",
                peer_id=peer_id,
                type=activity_type,
            )
            return True
        except Exception as e:
            logger.warning("Failed to set activity for peer %s: %s", peer_id, e)
            return False
