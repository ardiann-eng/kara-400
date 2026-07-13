"""Lazy public metadata bootstrap and per-user Bybit resource cleanup."""

from __future__ import annotations

import asyncio
from typing import Callable, Optional


class BybitSessionLifecycle:
    def __init__(self, client_factory: Callable[[], object]):
        self.client_factory = client_factory
        self.public_client = None
        self._lock = asyncio.Lock()

    async def ensure_public_client(self):
        if self.public_client:
            return self.public_client
        async with self._lock:
            if self.public_client:
                return self.public_client
            client = self.client_factory()
            try:
                await client.connect()
                await client.sync_clock()
                await client.load_instruments()
            except Exception:
                await client.close()
                raise
            self.public_client = client
            return client

    async def close_session(self, session) -> None:
        if not session:
            return
        private_ws = getattr(session, "bybit_ws", None)
        private_client = getattr(session, "bybit_client", None)
        errors = []
        if private_ws:
            try:
                await private_ws.stop()
            except Exception as exc:
                errors.append(exc)
        if private_client and private_client is not self.public_client:
            try:
                await private_client.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
