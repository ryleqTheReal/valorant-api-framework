"""
Lightweight event bus for the Valorant Stats App.

Events are registered as enum members, listeners are arbitrary callables.
Supports sync and async handlers, priorities, and one-shot listeners.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class Event(Enum):
    """All events that can occur in the system."""

    VALORANT_OPENED = "VALORANT_OPENED"         # Valorant process detected, lockfile read
    VALORANT_CLOSED = "VALORANT_CLOSED"         # Valorant process terminated
    AUTH_SUCCESS = "AUTH_SUCCESS"               # Riot auth succeeded
    AUTH_FAILED = "AUTH_FAILED"                 # Riot auth failed
    MATCH_STARTED = "MATCH_STARTED"             # New match detected
    MATCH_ENDED = "MATCH_ENDED"                 # Match finished
    DATA_COLLECTED = "DATA_COLLECTED"           # New stats data collected
    DATA_SENT = "DATA_SENT"                     # Data sent to remote server
    DATA_QUEUED = "DATA_QUEUED"                 # Data queued locally (offline)
    WEBSOCKET_EVENT = "WEBSOCKET_EVENT"         # Raw event from Riot Client WebSocket
    GAME_STATE_CHANGED = "GAME_STATE_CHANGED"   # sessionLoopState transition detected
    SHUTDOWN = "SHUTDOWN"                       # App is shutting down

IGNORE_EVENTS = ["AUTH_SUCCESS", "WEBSOCKET_EVENT"]

@dataclass
class Listener:
    callback: Callable[..., object]
    priority: int = 0       # Higher = executed first
    once: bool = False       # One-shot listener (removed after first call)
    name: str = ""           # Debug label




class EventBus:
    """
    Central event bus.

    Usage:
        bus = EventBus()
        bus.on(Event.VALORANT_CLOSED, my_handler)
        await bus.emit(Event.VALORANT_CLOSED, lockfile_data)
    """

    def __init__(self) -> None:
        self._listeners: dict[Event, list[Listener]] = {e: [] for e in Event}
        self._history: list[tuple[Event, object]] = []

    # -- Registration ----------------------------------------------------------

    def on(
        self,
        event: Event,
        callback: Callable[..., object],
        priority: int = 0,
        once: bool = False,
        name: str = "",
    ) -> Callable[..., object]:
        """Register a listener for an event."""
        
        listener = Listener(
            callback=callback,
            priority=priority,
            once=once,
            name=name or getattr(callback, "__name__", "anonymous"),
        )
        self._listeners[event].append(listener)
        self._listeners[event].sort(key=lambda ln: ln.priority, reverse=True)
        logger.debug(f"Registered listener '{listener.name}' for {event.name}")
        return callback

    def once(self, event: Event, callback: Callable[..., object], priority: int = 0) -> Callable[..., object]:
        """Register a one-shot listener (removed after first invocation)."""
        return self.on(event, callback, priority=priority, once=True)

    def off(self, event: Event, callback: Callable[..., object]) -> None:
        """Remove a listener."""
        self._listeners[event] = [
            ln for ln in self._listeners[event] if ln.callback != callback
        ]



    def listen(self, event: Event, priority: int = 0, once: bool = False):
        """
        Decorator for registering listeners:

            @bus.listen(Event.VALORANT_CLOSED)
            async def handle_lockfile(data):
                ...
        """
        def decorator(func: Callable[..., object]) -> Callable[..., object]:
            _ = self.on(event, func, priority=priority, once=once)
            return func
        return decorator

    async def emit(self, event: Event, data: object = None) -> list[object]:
        """
        Fire an event and invokes all registered listeners in priority order.
        Returns a list of return values from each listener.
        """
        
        if event.name not in IGNORE_EVENTS:
            logger.info(f"Event: {event.name}" + (f" | data={data}" if data else "")) 
        self._history.append((event, data))

        results: list[object] = []
        to_remove: list[Listener] = []

        for listener in self._listeners[event]:
            try:
                result: object
                if inspect.iscoroutinefunction(listener.callback):
                    result = await listener.callback(data)  # pyright: ignore[reportAny]
                else:
                    result = listener.callback(data)
                results.append(result)
            except Exception as e:
                logger.error(
                    f"Error in listener '{listener.name}' for {event.name}: {e}",
                    exc_info=True,
                )

            if listener.once:
                to_remove.append(listener)

        for listener in to_remove:
            self._listeners[event].remove(listener)

        return results




    # Debug helpers

    @property
    def history(self) -> list[tuple[Event, object]]:
        return self._history.copy()

    def listener_count(self, event: Event) -> int:
        return len(self._listeners[event])