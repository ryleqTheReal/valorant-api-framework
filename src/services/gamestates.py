"""
Game state tracker and data collector.

Monitors presence websocket events for sessionLoopState transitions,
collects relevant data from the Riot API for each transition,
and emits labeled DATA_COLLECTED events for downstream transport.
"""

from __future__ import annotations

import logging
from typing import Any

from services.auth_service import RiotSession
from services.event_bus import EventBus, Event
from utils.models import (
    EndpointURI,
    GameStateTransition,
    Presence,
    PresencePrivate,
    PresenceWebsocketEvent,
    SessionLoopState,
)

logger: logging.Logger = logging.getLogger(__name__)

# Lookup set for quick validation
_VALID_STATES: set[str] = {s.value for s in SessionLoopState}


class GamestateHandler:
    """
    Tracks sessionLoopState transitions and collects data for each.

    Listens for:
    - AUTH_SUCCESS      -> stores session (puuid + fetch capability)
    - WEBSOCKET_EVENT   -> detects state changes, triggers collection
    - VALORANT_CLOSED   -> resets state
    - SHUTDOWN          -> resets state
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus: EventBus = bus
        self._session: RiotSession | None = None
        self._current_state: SessionLoopState | None = None
        self._baseline_loadout: dict[str, Any] | None = None  # pyright: ignore[reportExplicitAny]
        self._register()

    @property
    def _puuid(self) -> str | None:
        return self._session.puuid if self._session else None

    def _register(self) -> None:
        """Subscribe to relevant events."""
        _ = self.bus.on(Event.AUTH_SUCCESS, self._on_auth_success, priority=5)
        _ = self.bus.on(Event.WEBSOCKET_EVENT, self._on_websocket_event, priority=5)
        _ = self.bus.on(Event.VALORANT_CLOSED, self._on_valorant_close, priority=5)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    # ------------------- Event Handlers -------------------

    async def _on_auth_success(self, data: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        """Store the authenticated session for API access."""
        self._session = data["session"]
        logger.info(f"Gamestate tracker ready for puuid {self._puuid}")

    async def _on_websocket_event(self, data: PresenceWebsocketEvent) -> None:
        """Check each presence event for a sessionLoopState change."""
        if self._session is None:
            return

        presence: Presence | None = self._find_own_presence(data)
        if presence is None:
            return

        new_state_str: str | None = self._extract_loop_state(presence)
        if new_state_str is None or new_state_str not in _VALID_STATES:
            return

        new_state = SessionLoopState(new_state_str)

        # No change
        if new_state == self._current_state:
            return

        previous = self._current_state
        self._current_state = new_state

        transition = GameStateTransition(
            previous=previous,
            current=new_state,
            puuid=self._puuid or "",
            presence=presence,
        )

        if previous is None:
            logger.info(f"Baseline state set: {new_state.value}")
        else:
            logger.info(f"State changed: {previous.value} -> {new_state.value}")

        await self._on_state_changed(transition)

    async def _on_valorant_close(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """Valorant closed -> reset tracking state."""
        self._reset()

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """App shutting down -> reset tracking state."""
        self._reset()

    # ------------------- State Change Dispatch -------------------

    async def _on_state_changed(self, transition: GameStateTransition) -> None:
        """Dispatch to the appropriate handler based on the new state."""
        match transition.current:
            case SessionLoopState.MENUS:
                await self._on_enter_menus(transition)
            case SessionLoopState.PREGAME:
                await self._on_enter_pregame(transition)
            case SessionLoopState.INGAME:
                await self._on_enter_ingame(transition)

    async def _on_enter_menus(self, transition: GameStateTransition) -> None:
        """Player returned to menus (e.g. match just ended).

        Collect post-match data here:
        - Match history / last match result
        - Updated MMR / rank
        - XP progress
        """
        assert self._session is not None
        # TODO: Implement
        pass

    async def _on_enter_pregame(self, transition: GameStateTransition) -> None:
        """Player entered agent select.

        Collect pre-match data here:
        - Pregame match info (map, mode, players)
        - Player loadouts / skins
        """
        assert self._session is not None
        # TODO: Implement
        pass

    async def _on_enter_ingame(self, transition: GameStateTransition) -> None:
        """Match started (loading screen / gameplay).

        Collect live match data here:
        - Current game match info
        - Player loadouts for the round
        - Hardware / performance stats
        """
        assert self._session is not None
        # TODO: Implement
        pass

    # ------------------- Helpers -------------------

    def _find_own_presence(self, event: PresenceWebsocketEvent) -> Presence | None:
        """Find the presence entry matching our puuid."""
        presences = event.data.data.presences
        if not presences:
            return None

        for presence in presences:
            if not isinstance(presence, Presence):
                continue
            if presence.puuid == self._puuid:
                return presence

        return None

    @staticmethod
    def _extract_loop_state(presence: Presence) -> str | None:
        """Extract sessionLoopState from a presence object."""
        private: PresencePrivate | str | None = presence.private
        if not isinstance(private, PresencePrivate):
            return None

        match_data = private.matchPresenceData
        if match_data is None:
            return None

        return match_data.sessionLoopState or None

    def _reset(self) -> None:
        """Clear all tracking state."""
        if self._current_state is not None:
            logger.info("Gamestate tracker reset")
        self._session = None
        self._current_state = None
