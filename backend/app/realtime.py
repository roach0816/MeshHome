"""Realtime invalidation hub. Events are hints to refetch REST state, never the source of truth."""

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CLIENT_QUEUE_SIZE = 100
# Queued for a socket whose session or API key was revoked: the socket closes with 4401.
REVOKED: dict[str, Any] = {"_close": 4401}


class Hub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[dict[str, Any] | None]] = set()
        # Who each socket belongs to ("session:<id>" or "api_key:<id>"), for revocation.
        self._owners: dict[asyncio.Queue, str] = {}

    def register(self, owner: str | None = None) -> asyncio.Queue[dict[str, Any] | None]:
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        self._clients.add(q)
        if owner:
            self._owners[q] = owner
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)
        self._owners.pop(q, None)

    def disconnect(self, *owners: str) -> None:
        """Close the open sockets of revoked sessions or API keys."""
        wanted = set(owners)
        for q, owner in list(self._owners.items()):
            if owner in wanted:
                self.unregister(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(REVOKED)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def publish(self, event_type: str, **data: Any) -> None:
        """Call only after the related database transaction has committed."""
        envelope = {"v": PROTOCOL_VERSION, "type": event_type, **data}
        for q in list(self._clients):
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                # Slow client: drop it rather than block collection. It will resync on reconnect.
                log.info("disconnecting slow websocket client")
                self.unregister(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    def close_all(self) -> None:
        for q in list(self._clients):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._clients.clear()
        self._owners.clear()


hub = Hub()
