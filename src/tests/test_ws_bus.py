"""WebSocket bus: off by default, and any Redis problem degrades to local delivery."""
import pytest

from src.core.bus import WsBus


class FakeRedis:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def publish(self, channel, payload):
        if self.fail:
            raise ConnectionError("redis down")
        self.sent.append((channel, payload))


@pytest.mark.asyncio
async def test_disabled_without_url_and_start_is_a_noop():
    bus = WsBus(url="")
    assert not bus.enabled
    await bus.start(lambda room, msg: None)
    assert bus._task is None
    assert await bus.publish("r1", {"type": "x"}) is False        # caller delivers locally


@pytest.mark.asyncio
async def test_publish_only_when_subscription_is_live():
    bus = WsBus(url="redis://x")
    bus._client = FakeRedis()
    assert await bus.publish("r1", {"n": 1}) is False             # not ready yet -> local delivery
    bus.ready = True
    assert await bus.publish("r1", {"n": 1, "when": __import__("datetime").datetime(2026, 1, 1)}) is True
    channel, payload = bus._client.sent[0]
    assert channel == "melbete:ws" and '"room_id": "r1"' in payload


@pytest.mark.asyncio
async def test_publish_failure_falls_back_to_local_delivery():
    bus = WsBus(url="redis://x")
    bus._client, bus.ready = FakeRedis(fail=True), True
    assert await bus.publish("r1", {"n": 1}) is False
