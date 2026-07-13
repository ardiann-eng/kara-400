import asyncio
from types import SimpleNamespace

import pytest

from core.bybit_session_lifecycle import BybitSessionLifecycle


class PublicClient:
    instances = []
    fail_load = False

    def __init__(self):
        self.calls = []
        self.closed = False
        self.symbol_registry = object()
        self.instances.append(self)

    async def connect(self):
        self.calls.append("connect")

    async def sync_clock(self):
        self.calls.append("sync_clock")

    async def load_instruments(self):
        self.calls.append("load_instruments")
        if self.fail_load:
            raise RuntimeError("metadata failed")

    async def close(self):
        self.calls.append("close")
        self.closed = True


@pytest.mark.asyncio
async def test_first_live_user_lazily_bootstraps_public_metadata_once():
    PublicClient.instances = []
    PublicClient.fail_load = False
    lifecycle = BybitSessionLifecycle(PublicClient)

    first, second = await asyncio.gather(
        lifecycle.ensure_public_client(),
        lifecycle.ensure_public_client(),
    )

    assert first is second
    assert len(PublicClient.instances) == 1
    assert first.calls == ["connect", "sync_clock", "load_instruments"]


@pytest.mark.asyncio
async def test_failed_metadata_bootstrap_closes_temporary_client():
    PublicClient.instances = []
    PublicClient.fail_load = True
    lifecycle = BybitSessionLifecycle(PublicClient)

    with pytest.raises(RuntimeError, match="metadata failed"):
        await lifecycle.ensure_public_client()

    assert lifecycle.public_client is None
    assert PublicClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_close_session_stops_ws_then_closes_private_rest():
    events = []

    class WS:
        async def stop(self):
            events.append("ws")

    class REST:
        async def close(self):
            events.append("rest")

    lifecycle = BybitSessionLifecycle(PublicClient)
    session = SimpleNamespace(bybit_ws=WS(), bybit_client=REST())

    await lifecycle.close_session(session)

    assert events == ["ws", "rest"]


@pytest.mark.asyncio
async def test_close_session_never_closes_shared_public_client():
    public = PublicClient()
    lifecycle = BybitSessionLifecycle(PublicClient)
    lifecycle.public_client = public
    session = SimpleNamespace(bybit_ws=None, bybit_client=public)

    await lifecycle.close_session(session)

    assert public.closed is False


@pytest.mark.asyncio
async def test_rest_cleanup_still_runs_when_ws_stop_fails():
    events = []

    class WS:
        async def stop(self):
            events.append("ws")
            raise RuntimeError("ws stop failed")

    class REST:
        async def close(self):
            events.append("rest")

    lifecycle = BybitSessionLifecycle(PublicClient)

    with pytest.raises(RuntimeError, match="ws stop failed"):
        await lifecycle.close_session(
            SimpleNamespace(bybit_ws=WS(), bybit_client=REST())
        )

    assert events == ["ws", "rest"]
