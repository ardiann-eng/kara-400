import asyncio
from types import SimpleNamespace

import pytest

from core.session_registry import SessionRegistry


@pytest.mark.asyncio
async def test_concurrent_get_session_builds_exactly_one_session(monkeypatch):
    sessions = {}
    registry = SessionRegistry(sessions)
    user = SimpleNamespace(chat_id="1")
    build_started = asyncio.Event()
    release_build = asyncio.Event()
    builds = []

    async def build(_user):
        builds.append(_user.chat_id)
        build_started.set()
        await release_build.wait()
        return SimpleNamespace(instance=len(builds))

    tasks = [asyncio.create_task(registry.get_or_create(
        "1", load_user=lambda chat_id: user, build=build
    )) for _ in range(5)]
    await build_started.wait()
    release_build.set()
    sessions = await asyncio.gather(*tasks)

    assert builds == ["1"]
    assert len({id(session) for session in sessions}) == 1
    assert sessions[0] is registry.sessions["1"]


@pytest.mark.asyncio
async def test_replace_session_is_atomic_against_parallel_get_session(monkeypatch):
    sessions = {}
    registry = SessionRegistry(sessions)
    old_session = SimpleNamespace(name="old")
    new_session = SimpleNamespace(name="new")
    sessions["1"] = old_session
    user = SimpleNamespace(chat_id="1")
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    builds = []

    class Lifecycle:
        async def close_session(self, session):
            assert session is old_session
            close_started.set()
            await release_close.wait()

    async def build(_user):
        builds.append(_user.chat_id)
        return new_session

    lifecycle = Lifecycle()
    replacing = asyncio.create_task(registry.replace(
        "1", load_user=lambda chat_id: user, build=build,
        close=lifecycle.close_session,
    ))
    await close_started.wait()
    readers = [asyncio.create_task(registry.get_or_create(
        "1", load_user=lambda chat_id: user, build=build
    )) for _ in range(4)]
    await asyncio.sleep(0)
    assert all(not reader.done() for reader in readers)

    release_close.set()
    replacement, *observed = await asyncio.gather(replacing, *readers)

    assert builds == ["1"]
    assert replacement is new_session
    assert all(session is new_session for session in observed)
    assert registry.sessions["1"] is new_session
