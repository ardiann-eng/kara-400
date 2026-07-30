from types import SimpleNamespace

import pytest

from execution.auto_entry import open_and_notify_auto_position


class Executor:
    def __init__(self, result):
        self.result = result

    async def open_position(self, signal):
        return self.result


class Telegram:
    def __init__(self):
        self.calls = []

    async def send_signal(self, signal, **kwargs):
        self.calls.append(("signal", signal, kwargs))

    async def send_position_opened(self, position, signal, **kwargs):
        self.calls.append(("opened", position, kwargs))


def signal():
    return SimpleNamespace(
        asset="BTC", side=SimpleNamespace(value="long"), score=70
    )


@pytest.mark.asyncio
async def test_failed_auto_open_sends_no_signal_or_position_message():
    telegram = Telegram()

    result = await open_and_notify_auto_position(
        Executor(None), telegram, signal(), "1"
    )

    assert result is None
    assert telegram.calls == []


@pytest.mark.asyncio
async def test_successful_auto_open_sends_signal_then_position_message():
    position = object()
    telegram = Telegram()

    result = await open_and_notify_auto_position(
        Executor(position), telegram, signal(), "1"
    )

    assert result is position
    assert [call[0] for call in telegram.calls] == ["signal", "opened"]
