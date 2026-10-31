"""Cross-container fan-out for live WebSocket messages.

Problem: a round's draw loop runs on ONE container (whichever holds its lease), but players' sockets
are spread over every container. Without this, players connected to another container would never
see a drawn number.

How: when REDIS_URL is set, `manager.broadcast` publishes the message to one Redis channel and EVERY
container (including the sender) delivers it to its own local sockets. When REDIS_URL is not set, or
Redis is unreachable, `publish` returns False and the caller delivers locally exactly as before - so
a single container behaves identically with or without Redis, and a Redis outage degrades to
"players on the same container as the draw loop", never to an error.
"""
import asyncio
import json
import logging
import os
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

CHANNEL = "melbete:ws"


class WsBus:
    def __init__(self, url: Optional[str] = None):
        self.url = (os.environ.get("REDIS_URL", "") if url is None else url).strip()
        self.ready = False            # True only while our own subscription is live
        self._client = None
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def start(self, deliver: Callable[[str, dict], Awaitable[None]]) -> None:
        """Begin listening. `deliver(room_id, message)` sends to this container's local sockets."""
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(deliver))

    async def _run(self, deliver) -> None:
        import redis.asyncio as aioredis  # imported here so a single-container deploy needs no Redis package at runtime

        backoff = 1.0
        while True:
            client = None
            try:
                client = aioredis.from_url(self.url, decode_responses=True, health_check_interval=30)
                pubsub = client.pubsub()
                await pubsub.subscribe(CHANNEL)
                self._client, self.ready, backoff = client, True, 1.0
                logger.info("WebSocket bus connected")
                async for item in pubsub.listen():
                    if item.get("type") != "message":
                        continue
                    try:
                        data = json.loads(item["data"])
                        await deliver(data["room_id"], data["message"])
                    except Exception:  # noqa: BLE001 - one bad message must not stop the listener
                        logger.exception("Could not deliver a bus message")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("WebSocket bus connection lost; delivering locally until it is back", exc_info=True)
            finally:
                self.ready, self._client = False, None
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def publish(self, room_id: str, message: dict) -> bool:
        """True = handed to Redis; every container (this one too) will deliver it via its subscription.
        False = the caller must deliver locally."""
        client = self._client
        if not (self.ready and client is not None):
            return False
        try:
            await client.publish(CHANNEL, json.dumps({"room_id": room_id, "message": message}, default=str))
            return True
        except Exception:  # noqa: BLE001
            logger.warning("WebSocket bus publish failed; delivering locally", exc_info=True)
            return False

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None


bus = WsBus()
