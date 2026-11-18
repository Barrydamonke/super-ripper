"""In-process pub/sub for server-sent events.

Each open /events stream subscribes with a bounded queue. Publishing never
blocks: a stream that falls behind is dropped, and the browser reconnects and
gets a fresh full state.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

Event = tuple[str, str]


class Notifier(Protocol):
    async def drive_changed(self, drive_id: str) -> None: ...
    async def guard_changed(self) -> None: ...


def format_sse(name: str, data: str) -> str:
    lines = data.split("\n")
    return f"event: {name}\n" + "".join(f"data: {line}\n" for line in lines) + "\n"


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[Event]
    dropped: bool = field(default=False)


class Broker:
    def __init__(self, maxsize: int = 200):
        self._maxsize = maxsize
        self._subs: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    @contextmanager
    def subscribe(self):
        sub = Subscription(asyncio.Queue(maxsize=self._maxsize))
        self._subs.add(sub)
        try:
            yield sub
        finally:
            self._subs.discard(sub)

    def publish(self, name: str, data: str) -> None:
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait((name, data))
            except asyncio.QueueFull:
                sub.dropped = True
                self._subs.discard(sub)
