"""
Lightweight event bus.

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

    # ---- Process / auth lifecycle ----
    RSO_LOGIN = "RSO_LOGIN"                             # Riot Client logged in (lockfile + RSO 200)
    RSO_LOGOUT = "RSO_LOGOUT"                           # Riot Client logged out (RSO non-200 / lockfile gone)
    VALORANT_OPENED = "VALORANT_OPENED"                 # Valorant process detected, lockfile read
    VALORANT_CLOSED = "VALORANT_CLOSED"                 # Valorant process terminated
    AUTH_SUCCESS = "AUTH_SUCCESS"                       # Riot auth succeeded
    AUTH_FAILED = "AUTH_FAILED"                         # Riot auth failed
    SHUTDOWN = "SHUTDOWN"                               # The application is shutting down

    # ---- Local websocket ----
    WEBSOCKET_CONNECTED = "WEBSOCKET_CONNECTED"         # Local WAMP socket connected and subscribed
    WEBSOCKET_DISCONNECTED = "WEBSOCKET_DISCONNECTED"   # Local WAMP socket closed
    WEBSOCKET_EVENT = "WEBSOCKET_EVENT"                 # Raw presence event from Riot Client websocket

    # ---- Game-session readiness ----
    INGAME_API_AVAILABLE = "INGAME_API_AVAILABLE"       # In-game endpoints reachable: first own-presence seen via poll or websocket. Ends on VALORANT_CLOSED.

    # ---- Game state machine ----
    GAME_STATE_CHANGED = "GAME_STATE_CHANGED"           # sessionLoopState transition (MENUS / PREGAME / INGAME)

    # ---- User / lobby activity ----
    USER_BECAME_IDLE = "USER_BECAME_IDLE"               # No menu activity for the configured idle window
    USER_BECAME_ACTIVE = "USER_BECAME_ACTIVE"           # Menu activity detected after being idle (or after first becoming active)
    USER_JOINED_QUEUE = "USER_JOINED_QUEUE"             # Party state transitioned to MATCHMAKING
    USER_LEFT_QUEUE = "USER_LEFT_QUEUE"                 # Party state transitioned from MATCHMAKING to anything else

    # ---- Self-data updates (auto-emitted on diff) ----
    LOADOUT_UPDATED = "LOADOUT_UPDATED"                 # User's equipped loadout version changed
    OWNED_ITEMS_UPDATED = "OWNED_ITEMS_UPDATED"         # Owned-item count changed
    USER_XP_UPDATED = "USER_XP_UPDATED"                 # Account XP version changed
    PENALTIES_UPDATED = "PENALTIES_UPDATED"             # Penalties version changed
    MMR_HISTORY_UPDATED = "MMR_HISTORY_UPDATED"         # MMR history version changed
    USERINFO_FETCHED = "USERINFO_FETCHED"               # User account info fetched (one-shot per session)
    ACCOUNT_ALIASES_FETCHED = "ACCOUNT_ALIASES_FETCHED" # Name/tag alias history fetched (one-shot per session)
    STORE_OFFERS_UPDATED = "STORE_OFFERS_UPDATED"       # Daily skin offers rotated

    # ---- Match data (live) ----
    PREGAME_MATCH_UPDATED = "PREGAME_MATCH_UPDATED"     # Pregame match data refreshed during agent select
    INGAME_MATCH_UPDATED = "INGAME_MATCH_UPDATED"       # Ingame match data fetched when a match starts
    INGAME_LOADOUTS_FETCHED = "INGAME_LOADOUTS_FETCHED" # Player loadouts fetched for the active match

    # ---- Friends (websocket-driven) ----
    FRIEND_ADDED = "FRIEND_ADDED"                       # Friend added via websocket
    FRIEND_REMOVED = "FRIEND_REMOVED"                   # Friend removed via websocket
    FRIEND_REQUEST_RECEIVED = "FRIEND_REQUEST_RECEIVED" # Incoming friend request
    FRIEND_REQUEST_SENT = "FRIEND_REQUEST_SENT"         # Outgoing friend request
    FRIENDS_LIST_FETCHED = "FRIENDS_LIST_FETCHED"       # Initial friend list fetched on presence baseline


# Events that fire at high frequency (multiple times per second). Logged at DEBUG instead of INFO
# so the default log stream stays readable.
_HIGH_FREQUENCY_EVENTS: frozenset[Event] = frozenset({Event.WEBSOCKET_EVENT})


@dataclass
class Listener:
    callback: Callable[..., object]
    priority: int = 0   # Higher = executed first
    once: bool = False  # One-shot listener (removed after first call)
    name: str = ""      # Debug label


class EventBus:
    """Central event bus.

    Usage:
        bus = EventBus()
        bus.on(Event.VALORANT_CLOSED, my_handler)
        await bus.emit(Event.VALORANT_CLOSED, lockfile_data)
    """

    def __init__(self) -> None:
        self._listeners: dict[Event, list[Listener]] = {e: [] for e in Event}

    # -- Registration ---------------------------------------------------------

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
        """Decorator form of :meth:`on`.

            @bus.listen(Event.VALORANT_CLOSED)
            async def handle_close(data):
                ...
        """
        def decorator(func: Callable[..., object]) -> Callable[..., object]:
            _ = self.on(event, func, priority=priority, once=once)
            return func
        return decorator

    # -- Emission -------------------------------------------------------------

    async def emit(self, event: Event, data: object = None) -> list[object]:
        """Fire an event and invoke all registered listeners in priority order.

        Returns a list of return values from each listener.
        """
        log_level = logging.DEBUG if event in _HIGH_FREQUENCY_EVENTS else logging.INFO
        logger.log(log_level, f"Event: {event.name}" + (f" | data={data}" if data is not None else ""))

        results: list[object] = []
        to_remove: list[Listener] = []

        for listener in self._listeners[event]:
            try:
                if inspect.iscoroutinefunction(listener.callback):
                    result: object = await listener.callback(data)  # pyright: ignore[reportAny]
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

    def listener_count(self, event: Event) -> int:
        return len(self._listeners[event])