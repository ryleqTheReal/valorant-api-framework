"""
Game-state tracker.

Listens to presence websocket events and presence polling to detect
sessionLoopState transitions (MENUS / PREGAME / INGAME). Dispatches
per-state work, runs the menu activity / idle / queue monitor, and
auto-fetches diff-based events for self-data (loadout, owned items,
XP, penalties, MMR) and the storefront.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable
from collections.abc import Awaitable

import httpx

from services.auth_service import RiotSession
from services.event_bus import EventBus, Event
from services.request_scheduler import RequestScheduler
from utils.models import (
    GameStateTransition,
    LockfileData,
    Presence,
    PresencePrivate,
    PresenceWebsocketEvent,
    SessionLoopState,
    StorefrontResponse,
    _MatchPresenceData,  # pyright: ignore[reportPrivateUsage]
    _PartyPresenceData,  # pyright: ignore[reportPrivateUsage]
)

logger: logging.Logger = logging.getLogger(__name__)

# Lookup set for quick validation
_VALID_STATES: set[str] = {s.value for s in SessionLoopState}

_MENU_IDLE_TIMEOUT: float = 60.0


class GamestateHandler:
    """
    Tracks sessionLoopState transitions and dispatches per-state work.

    Listens for:
    - AUTH_SUCCESS    -> stores session, starts scheduler + auto-polls
    - VALORANT_OPENED -> begins presence polling for the initial state
    - WEBSOCKET_EVENT -> detects state changes, runs per-state monitors
    - VALORANT_CLOSED -> clears game-state but keeps the session alive
    - RSO_LOGOUT      -> full reset
    - SHUTDOWN        -> full reset
    """

    def __init__(self, bus: EventBus, scheduler: RequestScheduler) -> None:
        self.bus: EventBus = bus
        self._scheduler: RequestScheduler = scheduler
        self._session: RiotSession | None = None

        # Game-state tracking
        self._current_state: SessionLoopState | None = None
        self._pending_task: asyncio.Task[None] | None = None
        self._presence_poll_task: asyncio.Task[None] | None = None
        self._pregame_poll_task: asyncio.Task[None] | None = None
        self._store_poll_task: asyncio.Task[None] | None = None
        self._valorant_open: bool = False

        # Active match
        self._active_match_id: str | None = None
        self._active_queue_id: str | None = None

        # Diff caches (auto-emit on change)
        self._loadout_version: int | None = None
        self._owned_item_count: int | None = None
        self._xp_version: int | None = None
        self._penalties_version: int | None = None
        self._mmr_version: int | None = None

        # Menu activity / queue tracking
        self._menu_idle_timer: asyncio.TimerHandle | None = None
        self._user_idle: bool = False
        self._last_activity_snapshot: dict[str, object] | None = None
        self._was_in_queue: bool = False

        # In-game API readiness (one-shot per Valorant session)
        self._ingame_api_emitted: bool = False

        self._register()

    @property
    def _puuid(self) -> str | None:
        return self._session.puuid if self._session else None

    def _register(self) -> None:
        """Subscribe to relevant events."""
        _ = self.bus.on(Event.AUTH_SUCCESS, self._on_auth_success, priority=5)
        _ = self.bus.on(Event.VALORANT_OPENED, self._on_valorant_open, priority=5)
        _ = self.bus.on(Event.WEBSOCKET_EVENT, self._on_websocket_event, priority=5)
        _ = self.bus.on(Event.VALORANT_CLOSED, self._on_valorant_close, priority=5)
        _ = self.bus.on(Event.RSO_LOGOUT, self._on_rso_logout, priority=5)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    # ------------------- Event Handlers -------------------

    async def _on_auth_success(self, data: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        """Store the authenticated session and start the auto-polls.

        Auto-polling begins as soon as we are authenticated; there is no
        offset window. The scheduler's rate limiter paces requests.
        """
        self._session = data["session"]
        self._scheduler.start()
        logger.info(f"Gamestate tracker ready for puuid {self._puuid}")

        # Initial baseline of self-data
        self._enqueue_general_checks()

        # Background storefront polling (sleeps until next rotation between fetches)
        self._store_poll_task = asyncio.create_task(self._poll_store())

        # VALORANT_OPENED may have fired before the session was ready,
        # in which case the presence poll was skipped. Start it now.
        if self._valorant_open and self._current_state is None:
            logger.info("Valorant already open at auth time, starting presence poll now")
            self._presence_poll_task = asyncio.create_task(self._poll_initial_presence())

    async def _on_valorant_open(self, data: LockfileData) -> None:  # pyright: ignore[reportUnusedParameter]
        """Valorant launched -> poll for the initial gamestate.

        The presence endpoint is local (not rate-limited), so we start
        polling immediately while waiting for the websocket to surface
        the same data. Whichever path arrives first wins.
        """
        self._valorant_open = True
        if self._session is None or self._current_state is not None:
            return

        self._presence_poll_task = asyncio.create_task(self._poll_initial_presence())

    async def _poll_initial_presence(self) -> None:
        """Poll /chat/v4/presences until we find our own sessionLoopState."""
        if self._session is None:
            return

        logger.info("Polling /chat/v4/presences for initial gamestate...")

        while self._current_state is None:
            try:
                presence_data = await self._session.local_get_presences()

                if presence_data.presences:
                    for p in presence_data.presences:
                        if not isinstance(p, Presence):
                            continue
                        if p.puuid != self._puuid or p.product != "valorant":
                            continue

                        # First own-presence ever this Valorant session -> mark API available
                        self._maybe_emit_ingame_api_available()

                        state_str = self._extract_loop_state(p)
                        if state_str and state_str in _VALID_STATES:
                            new_state = SessionLoopState(state_str)
                            self._current_state = new_state

                            # Set activity baseline from poll so menus
                            # monitoring can detect changes from the
                            # very first websocket event
                            if new_state == SessionLoopState.MENUS:
                                private = p.private
                                if isinstance(private, PresencePrivate):
                                    self._last_activity_snapshot = self._build_activity_snapshot(private)
                                    self._was_in_queue = self._is_in_queue(private)
                                    logger.info("Activity snapshot baseline set from presence poll")

                            transition = GameStateTransition(
                                previous=None,
                                current=new_state,
                                puuid=self._puuid or "",
                                presence=p,
                            )
                            logger.info(f"Initial gamestate from presence poll: {new_state.value}")
                            await self._on_state_changed(transition)
                            await self._fetch_initial_friends()
                            await self._fetch_userinfo()
                            return

            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as e:
                logger.warning(
                    f"Presence poll failed: {e.response.status_code} {e.response.text[:200]}"
                )
            except Exception as e:
                logger.warning(f"Presence poll failed: {type(e).__name__}: {e}")

            await asyncio.sleep(5)

        if self._current_state is None:  # pyright: ignore[reportUnnecessaryComparison]
            logger.debug("Presence polling stopped (session closed or state already set)")

    async def _on_websocket_event(self, data: PresenceWebsocketEvent) -> None:
        """Check each presence event for a sessionLoopState change."""
        if self._session is None:
            return

        presence: Presence | None = self._find_own_presence(data)
        if presence is None:
            return

        # First own-presence ever this Valorant session -> mark API available
        self._maybe_emit_ingame_api_available()

        new_state_str: str | None = self._extract_loop_state(presence)
        if new_state_str is None or new_state_str not in _VALID_STATES:
            return

        new_state = SessionLoopState(new_state_str)

        # No state change, but still process presence for active monitors
        if new_state == self._current_state:
            private = presence.private
            if isinstance(private, PresencePrivate):
                party = private.partyPresenceData
                party_state = party.partyState if isinstance(party, _PartyPresenceData) else None
                logger.info(f"Own presence update (no state change): partyState={party_state}, queueId={private.queueId}")
                self._process_presence_for_state(private)
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
            # Poll lost the race, set activity snapshot from this websocket
            # event so menus monitoring has a baseline to compare against
            if new_state == SessionLoopState.MENUS:
                private = presence.private
                if isinstance(private, PresencePrivate):
                    self._last_activity_snapshot = self._build_activity_snapshot(private)
                    self._was_in_queue = self._is_in_queue(private)
                    logger.info("Activity snapshot baseline set from websocket (poll lost race)")
            await self._fetch_initial_friends()
            await self._fetch_userinfo()
        else:
            logger.info(f"State changed: {previous.value} -> {new_state.value}")

        await self._on_state_changed(transition)

    async def _on_valorant_close(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """Valorant closed -> clear game-state tracking but keep the session alive.

        The session and scheduler survive because the user is still
        logged into the Riot Client. Only RSO_LOGOUT/SHUTDOWN tears those down.
        """
        self._valorant_open = False
        self._ingame_api_emitted = False
        self._clear_game_state()

    async def _on_rso_logout(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """Riot Client logged out -> reset tracking state."""
        self._reset()

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """App shutting down -> reset tracking state."""
        self._reset()

    # ------------------- State Change Dispatch -------------------

    async def _on_state_changed(self, transition: GameStateTransition) -> None:
        """Dispatch to the per-state handler and emit GAME_STATE_CHANGED.

        Cancels in-flight per-state work and tells the scheduler to purge
        stale state-bound requests before dispatching the new handler.
        """
        self._cancel_pending_task()
        self._cancel_pregame_poll()
        self._cancel_menu_idle_timer()
        self._active_queue_id = None
        # Clears the state queue and cancels any in-flight state request
        self._scheduler.on_state_change()

        _ = await self.bus.emit(Event.GAME_STATE_CHANGED, transition)

        # First presence received -> fetch account aliases once
        if transition.previous is None:
            _ = asyncio.create_task(self._fetch_account_aliases())

        match transition.current:
            case SessionLoopState.MENUS:
                self._pending_task = asyncio.create_task(self._on_enter_menus(transition))
            case SessionLoopState.PREGAME:
                self._pending_task = asyncio.create_task(self._on_enter_pregame(transition))
            case SessionLoopState.INGAME:
                self._pending_task = asyncio.create_task(self._on_enter_ingame(transition))

    async def _on_enter_menus(self, transition: GameStateTransition) -> None:
        """Player entered menus.

        Starts the idle timer. If returning from a match, refreshes
        self-data so post-game changes (XP, MMR, ...) propagate.
        """
        if not self._session:
            return

        self._user_idle = False
        self._reset_menu_idle_timer()

        if transition.previous == SessionLoopState.INGAME:
            # Coming back from a match; refresh self-data
            self._enqueue_general_checks()

    async def _on_enter_pregame(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """Player entered agent select.

        GLZ endpoints are not rate-limited, so we spawn a 1s polling
        coroutine that runs independently of the scheduler.
        """
        if not self._session:
            return

        self._pregame_poll_task = asyncio.create_task(self._poll_pregame_match())

    async def _on_enter_ingame(self, transition: GameStateTransition) -> None:
        """Match started (loading screen / gameplay).

        GLZ ingame endpoints are not rate-limited, so we fetch the match
        data once directly and store the match ID for the duration of the
        match. Self-data (loadout, MMR, ...) is refreshed in the
        background through the scheduler.
        """
        if not self._session:
            return

        # Determine the active queue for downstream consumers
        private = transition.presence.private if transition.presence else None
        if isinstance(private, PresencePrivate):
            self._active_queue_id = private.queueId

        await self._fetch_ingame_match()
        await self._fetch_ingame_loadouts()

        self._enqueue_general_checks()

    # ------------------- Per-State Monitors -------------------

    def _process_presence_for_state(self, private: PresencePrivate) -> None:
        """Route presence updates to the appropriate monitor for the current state."""
        if self._current_state == SessionLoopState.MENUS:
            self._on_menus_presence_update(private)

    def _on_menus_presence_update(self, private: PresencePrivate) -> None:
        """Track menu activity, queue join/leave, and idle transitions.

        Three concerns are folded together because they all key off the
        same presence diff:
        1. Joining or leaving the matchmaking queue emits its own event
           and resets activity tracking.
        2. While in the queue, no idle timer runs (the user is busy).
        3. Outside of the queue, any presence diff resets the idle timer
           and, if the user was previously idle, emits USER_BECAME_ACTIVE.
        """
        in_queue = self._is_in_queue(private)

        # Queue join: in_queue True after being False
        if in_queue and not self._was_in_queue:
            self._was_in_queue = True
            self._cancel_menu_idle_timer()
            self._user_idle = False
            self._last_activity_snapshot = None
            logger.info("User joined the queue")
            _ = asyncio.ensure_future(self.bus.emit(Event.USER_JOINED_QUEUE))
            return

        # Queue leave: was in queue, now isn't
        if not in_queue and self._was_in_queue:
            self._was_in_queue = False
            self._user_idle = False
            self._last_activity_snapshot = self._build_activity_snapshot(private)
            self._reset_menu_idle_timer()
            logger.info("User left the queue")
            _ = asyncio.ensure_future(self.bus.emit(Event.USER_LEFT_QUEUE))
            return

        # In-queue updates beyond the join transition are not interesting
        if in_queue:
            return

        snapshot = self._build_activity_snapshot(private)

        if self._last_activity_snapshot is None or snapshot != self._last_activity_snapshot:
            if self._user_idle:
                self._user_idle = False
                logger.info("Menu activity detected after idle")
                _ = asyncio.ensure_future(self.bus.emit(Event.USER_BECAME_ACTIVE))
            else:
                logger.info("Menu activity detected, resetting idle timer")
            self._reset_menu_idle_timer()

        self._last_activity_snapshot = snapshot

    @staticmethod
    def _is_in_queue(private: PresencePrivate) -> bool:
        party = private.partyPresenceData
        return isinstance(party, _PartyPresenceData) and party.partyState == "MATCHMAKING"

    @staticmethod
    def _build_activity_snapshot(private: PresencePrivate) -> dict[str, object]:
        """Extract fields that indicate user activity in menus."""
        party = private.partyPresenceData
        return {
            "queueId": private.queueId,
            "partySize": private.partySize,
            "partyState": party.partyState if isinstance(party, _PartyPresenceData) else None,
            "partyId": private.partyId,
            "queueEntryTime": party.queueEntryTime if isinstance(party, _PartyPresenceData) else None,
            "isIdle": private.isIdle,
        }

    def _reset_menu_idle_timer(self) -> None:
        """Cancel any existing idle timer and start a new one."""
        self._cancel_menu_idle_timer()
        loop = asyncio.get_running_loop()
        self._menu_idle_timer = loop.call_later(_MENU_IDLE_TIMEOUT, self._on_menu_idle_expired)

    def _cancel_menu_idle_timer(self) -> None:
        """Cancel the menu idle timer if running."""
        if self._menu_idle_timer is not None:
            self._menu_idle_timer.cancel()
            self._menu_idle_timer = None

    def _on_menu_idle_expired(self) -> None:
        """Called after the configured idle window with no activity."""
        if self._current_state != SessionLoopState.MENUS or self._user_idle:
            return
        self._user_idle = True
        logger.info(f"Menu idle timer fired ({_MENU_IDLE_TIMEOUT:.0f}s of no activity)")
        _ = asyncio.ensure_future(self.bus.emit(Event.USER_BECAME_IDLE))

    # ------------------- INGAME_API_AVAILABLE -------------------

    def _maybe_emit_ingame_api_available(self) -> None:
        """Emit INGAME_API_AVAILABLE once per Valorant session.

        Resets on VALORANT_CLOSED so the next launch fires it again.
        """
        if self._ingame_api_emitted:
            return
        self._ingame_api_emitted = True
        logger.info("In-game API available (first own-presence detected)")
        _ = asyncio.ensure_future(self.bus.emit(Event.INGAME_API_AVAILABLE))

    # ------------------- General Checks -------------------

    def _enqueue_general_checks(self) -> None:
        """Enqueue all diff-checked self-data fetches (low priority)."""
        if self._session is None:
            return
        self._scheduler.enqueue_general(self._check_owned, "owned items")
        self._scheduler.enqueue_general(self._check_loadout, "loadout")
        self._scheduler.enqueue_general(self._check_xp, "xp")
        self._scheduler.enqueue_general(self._check_penalties, "penalties")
        self._scheduler.enqueue_general(self._check_mmr, "mmr")

    async def _check_and_emit[T](
        self,
        fetch: Callable[[], Awaitable[T]],
        get_key: Callable[[T], Any],  # pyright: ignore[reportExplicitAny]
        cache_attr: str,
        event: Event,
        label: str,
    ) -> None:
        """Generic check-diff-emit pattern.

        Fetches data, compares a key value against the cached one, and
        emits an event if it changed.
        """
        if not self._session:
            return

        try:
            data: T = await fetch()
        except Exception as e:
            logger.warning(f"Failed to fetch {label}: {e}")
            return

        new_key: Any = get_key(data)  # pyright: ignore[reportExplicitAny, reportAny]
        old_key: Any = getattr(self, cache_attr)  # pyright: ignore[reportExplicitAny, reportAny]

        if new_key == old_key:
            return

        setattr(self, cache_attr, new_key)

        if old_key is None:
            logger.info(f"Baseline {label} set: {new_key}")
        else:
            logger.info(f"{label} changed: {old_key} -> {new_key}")

        _ = await self.bus.emit(event, data)

    async def _check_loadout(self) -> None:
        """Checks whether the user has updated their loadout."""
        await self._check_and_emit(
            fetch=self._session.general_get_loadout,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda loadout: loadout.Version,
            cache_attr="_loadout_version",
            event=Event.LOADOUT_UPDATED,
            label="loadout version",
        )

    async def _check_owned(self) -> None:
        """Checks whether the user has acquired new items."""
        await self._check_and_emit(
            fetch=self._session.general_get_owned,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda items: items.item_count,
            cache_attr="_owned_item_count",
            event=Event.OWNED_ITEMS_UPDATED,
            label="owned items count",
        )

    async def _check_xp(self) -> None:
        """Checks whether the user has gained XP."""
        await self._check_and_emit(
            fetch=self._session.general_get_xp,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda xp: xp.Version,
            cache_attr="_xp_version",
            event=Event.USER_XP_UPDATED,
            label="user xp version",
        )

    async def _check_penalties(self) -> None:
        """Checks whether the user has new penalties."""
        await self._check_and_emit(
            fetch=self._session.general_get_penalties,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda p: p.Version,
            cache_attr="_penalties_version",
            event=Event.PENALTIES_UPDATED,
            label="user penalties version",
        )

    async def _check_mmr(self) -> None:
        """Checks whether the user's MMR history has updated."""
        await self._check_and_emit(
            fetch=self._session.general_get_mmr,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda mmr: mmr.Version,
            cache_attr="_mmr_version",
            event=Event.MMR_HISTORY_UPDATED,
            label="user mmr version",
        )

    # ------------------- One-Shot Local Fetches -------------------

    async def _fetch_initial_friends(self) -> None:
        """Fetch the full friend list once after the first presence is received."""
        if not self._session:
            return
        try:
            friends = await self._session.local_get_friends()
            _ = await self.bus.emit(Event.FRIENDS_LIST_FETCHED, friends)
        except Exception:
            logger.warning("Failed to fetch initial friend list", exc_info=True)

    async def _fetch_userinfo(self) -> None:
        """Fetch the user's account info once after the first presence is received."""
        if not self._session:
            return
        try:
            userinfo = await self._session.local_get_userinfo()
            _ = await self.bus.emit(Event.USERINFO_FETCHED, userinfo)
        except Exception:
            logger.warning("Failed to fetch userinfo", exc_info=True)

    async def _fetch_account_aliases(self) -> None:
        """Fetch the player's name/tag alias history once (local, not rate-limited)."""
        if not self._session:
            return
        try:
            aliases = await self._session.local_get_aliases()
            _ = await self.bus.emit(Event.ACCOUNT_ALIASES_FETCHED, aliases)
            logger.info(f"Account aliases fetched: {len(aliases)} alias(es)")
        except Exception as e:
            logger.warning(f"Failed to fetch account aliases: {type(e).__name__}: {e}")

    # ------------------- GLZ Match Polling -------------------

    async def _poll_pregame_match(self) -> None:
        """Poll pregame match data at 1s intervals (GLZ is not rate-limited).

        Gets the match ID once, then polls the match endpoint every second.

        Emits PREGAME_MATCH_UPDATED whenever the 'Version' field changes.

        Stops on 404 (pregame ended server-side) or cancellation (state change).
        """
        if not self._session:
            return

        try:
            match_id = await self._session.pregame_get_player()
            if not match_id:
                logger.warning("No pregame match ID returned")
                return

            logger.info(f"Pregame poll started for match {match_id}")
            pregame_version: int | None = None

            while True:
                try:
                    match_data = await self._session.pregame_get_match(match_id)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        logger.info("Pregame poll ended: 404 (match no longer in pregame)")
                        return
                    raise

                if match_data.Version != pregame_version:
                    pregame_version = match_data.Version
                    _ = await self.bus.emit(Event.PREGAME_MATCH_UPDATED, match_data)
                    logger.debug(f"Pregame match updated: version {pregame_version}")

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info("Pregame poll cancelled (state changed)")
        except Exception as e:
            logger.warning(f"Pregame poll failed: {type(e).__name__}: {e}")

    async def _fetch_ingame_match(self) -> None:
        """Fetch ingame match data once (GLZ is not rate-limited).

        Gets the match ID, stores it on self._active_match_id, fetches
        the full match data, and emits INGAME_MATCH_UPDATED.
        """
        if not self._session:
            return

        try:
            match_id = await self._session.ingame_get_player()
            if not match_id:
                logger.warning("No ingame match ID returned")
                return

            self._active_match_id = match_id
            match_data = await self._session.ingame_get_match(match_id)
            _ = await self.bus.emit(Event.INGAME_MATCH_UPDATED, match_data)
            logger.info(f"Ingame match loaded: {match_id}")
        except Exception as e:
            logger.warning(f"Failed to fetch ingame match: {type(e).__name__}: {e}")

    async def _fetch_ingame_loadouts(self) -> None:
        """Fetch player loadouts for the active match (GLZ is not rate-limited).

        Retries with a 2s delay until successful, the match ends, or the
        coroutine is cancelled. This is the most valuable per-match data,
        so we don't give up while the match is still active.
        """
        if not self._session or not self._active_match_id:
            return

        match_id = self._active_match_id

        while True:
            try:
                loadouts = await self._session.ingame_get_loadouts(match_id)
                _ = await self.bus.emit(Event.INGAME_LOADOUTS_FETCHED, loadouts)
                logger.info(f"Ingame loadouts fetched for match {match_id}")
                return
            except asyncio.CancelledError:
                logger.info("Ingame loadouts fetch cancelled (state changed)")
                raise
            except Exception as e:
                logger.warning(f"Failed to fetch ingame loadouts: {type(e).__name__}: {e}, retrying in 2s")
                await asyncio.sleep(2)

                # Check if match is still active (state may have changed during sleep)
                if self._active_match_id != match_id:
                    logger.info("Ingame loadouts fetch aborted: match ended")
                    return

    # ------------------- Storefront Polling -------------------

    async def _poll_store(self) -> None:
        """Fetch the storefront, emit, then sleep until the offers rotate. Repeats until cancelled.

        The actual API call is enqueued through the scheduler so it
        counts against the shared rate-limit budget.
        """
        if not self._session:
            return

        try:
            while True:
                future: asyncio.Future[StorefrontResponse] = asyncio.get_running_loop().create_future()

                async def _fetch_store(fut: asyncio.Future[StorefrontResponse] = future) -> None:
                    try:
                        result = await self._session.general_get_store()  # pyright: ignore[reportOptionalMemberAccess]
                        fut.set_result(result)
                    except Exception as e:
                        fut.set_exception(e)

                self._scheduler.enqueue_general(_fetch_store, "store offers")

                try:
                    store = await future
                except Exception as e:
                    logger.warning(f"Store fetch failed: {type(e).__name__}: {e}, retrying in 60s")
                    await asyncio.sleep(60)
                    continue

                _ = await self.bus.emit(Event.STORE_OFFERS_UPDATED, store)
                remaining = store.single_item_offers_remaining_seconds
                if remaining is None or remaining <= 0:
                    logger.warning("Store rotation timer missing or expired, retrying in 60s")
                    await asyncio.sleep(60)
                    continue

                logger.info(f"Store offers fetched, next rotation in {remaining}s")
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            logger.info("Store poll cancelled")

    # ------------------- Helpers -------------------

    def _find_own_presence(self, event: PresenceWebsocketEvent) -> Presence | None:
        """Find the presence entry matching our puuid."""
        presences = event.data.data.presences
        if not presences:
            return None

        for presence in presences:
            if not isinstance(presence, Presence):
                continue
            if presence.puuid == self._puuid and presence.product == "valorant":
                return presence

        return None

    @staticmethod
    def _extract_loop_state(presence: Presence) -> str | None:
        """Extract sessionLoopState from a presence object."""
        private: PresencePrivate | str | None = presence.private
        if not isinstance(private, PresencePrivate):
            return None

        match_data = private.matchPresenceData
        if not isinstance(match_data, _MatchPresenceData):
            return None

        return match_data.sessionLoopState or None

    def _cancel_pending_task(self) -> None:
        """Cancel any in-flight per-state task."""
        if self._pending_task and not self._pending_task.done():
            _ = self._pending_task.cancel()
            logger.info("Cancelled pending per-state task (state changed)")
        self._pending_task = None

    def _cancel_pregame_poll(self) -> None:
        """Cancel the pregame polling coroutine if running."""
        if self._pregame_poll_task and not self._pregame_poll_task.done():
            _ = self._pregame_poll_task.cancel()
        self._pregame_poll_task = None

    def _clear_game_state(self) -> None:
        """Clear game-state tracking without touching the session.

        Used when Valorant closes but the Riot Client is still logged in.
        The scheduler keeps running so general-queue requests keep flowing.
        """
        if self._presence_poll_task and not self._presence_poll_task.done():
            _ = self._presence_poll_task.cancel()
            self._presence_poll_task = None
        self._cancel_pregame_poll()
        self._cancel_menu_idle_timer()
        self._last_activity_snapshot = None
        self._was_in_queue = False
        self._user_idle = False
        # Purge state-bound requests but keep the scheduler alive
        self._scheduler.on_state_change()
        self._cancel_pending_task()
        if self._current_state is not None:
            logger.info("Game state cleared (Valorant closed, session still active)")
        self._current_state = None
        self._active_match_id = None
        self._active_queue_id = None
        self._loadout_version = None
        self._owned_item_count = None
        self._xp_version = None
        self._penalties_version = None
        self._mmr_version = None

    def _reset(self) -> None:
        """Full teardown: clear everything including the session and scheduler.

        Used on RSO_LOGOUT and SHUTDOWN when the session is no longer valid.
        """
        self._clear_game_state()
        if self._store_poll_task and not self._store_poll_task.done():
            _ = self._store_poll_task.cancel()
            self._store_poll_task = None
        self._scheduler.stop()
        if self._session is not None:
            logger.info("Gamestate tracker reset (session ended)")
        self._session = None
        self._ingame_api_emitted = False
        self._valorant_open = False
