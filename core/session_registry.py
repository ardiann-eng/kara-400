"""Concurrency-safe ownership of per-user runtime sessions."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Optional


class SessionRegistry:
    def __init__(self, sessions: Dict[str, object]):
        self.sessions = sessions
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, chat_id: str) -> asyncio.Lock:
        return self._locks.setdefault(str(chat_id), asyncio.Lock())

    async def get_or_create(
        self,
        chat_id: str,
        *,
        load_user: Callable[[str], object],
        build: Callable[[object], Awaitable[object]],
    ) -> Optional[object]:
        chat_id = str(chat_id)
        async with self._lock(chat_id):
            current = self.sessions.get(chat_id)
            if current is not None:
                return current
            user = load_user(chat_id)
            if user is None:
                return None
            session = await build(user)
            self.sessions[chat_id] = session
            return session

    async def close(
        self,
        chat_id: str,
        *,
        close: Callable[[object], Awaitable[None]],
    ) -> Optional[object]:
        chat_id = str(chat_id)
        async with self._lock(chat_id):
            current = self.sessions.pop(chat_id, None)
            if current is not None:
                await close(current)
            return current

    async def replace(
        self,
        chat_id: str,
        *,
        load_user: Callable[[str], object],
        build: Callable[[object], Awaitable[object]],
        close: Callable[[object], Awaitable[None]],
    ) -> Optional[object]:
        chat_id = str(chat_id)
        async with self._lock(chat_id):
            current = self.sessions.pop(chat_id, None)
            if current is not None:
                await close(current)
            user = load_user(chat_id)
            if user is None:
                return None
            session = await build(user)
            self.sessions[chat_id] = session
            return session
