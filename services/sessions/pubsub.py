from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from services.db.models import Event


@dataclass(slots=True)
class SessionEventHub:
    _subscribers: dict[UUID, set[asyncio.Queue[Event]]] = field(default_factory=dict)

    async def publish(self, session_id: UUID, event: Event) -> None:
        for queue in list(self._subscribers.get(session_id, set())):
            await queue.put(event)

    def subscribe(self, session_id: UUID) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.setdefault(session_id, set()).add(queue)
        return queue

    def unsubscribe(self, session_id: UUID, queue: asyncio.Queue[Event]) -> None:
        subscribers = self._subscribers.get(session_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(session_id, None)
