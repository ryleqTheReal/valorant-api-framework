"""
Two-tier priority scheduler for Riot API requests.

Manages two queues with different lifecycle behaviors:

- **State queue** (high priority): Requests bound to the current game state
  (e.g. pregame match info, core-game data). Purged when the loop state
  changes because they would only fail and burn rate-limit budget.
- **General queue** (low priority): Persistent requests that survive state
  changes (e.g. loadout, owned items, XP). Only processed once the state
  queue is empty.

A single worker coroutine drains the state queue first, then the general
queue. Every outgoing request waits for a slot from a fixed-window
rate limiter (default 30 req/min, configurable).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from collections.abc import Awaitable, Callable

logger: logging.Logger = logging.getLogger(__name__)


class RateLimiter:
    """Fixed-window per-minute request limiter.

    Call :meth:`wait_for_slot` before every outgoing request; it will
    ``asyncio.sleep`` until the current window has budget.
    """

    def __init__(self, requests_per_minute: int = 30, window_seconds: float = 60.0) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self._limit: int = requests_per_minute
        self._window_seconds: float = window_seconds
        self._request_count: int = 0
        self._window_start: float = 0.0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def remaining(self) -> int:
        """Requests remaining in the current window."""
        self._slide_window_if_expired(time.monotonic())
        return max(0, self._limit - self._request_count)

    async def wait_for_slot(self) -> None:
        """Block until a request slot is available, then consume it."""
        now = time.monotonic()
        if self._window_start == 0.0:
            self._window_start = now
            self._request_count = 0

        self._slide_window_if_expired(now)

        if self._request_count >= self._limit:
            wait = self._window_seconds - (time.monotonic() - self._window_start)
            if wait > 0:
                logger.info(f"Rate limit reached ({self._request_count}/{self._limit}), waiting {wait:.1f}s for next window")
                await asyncio.sleep(wait)
            self._slide_window()

        self._request_count += 1

    def _slide_window_if_expired(self, now: float) -> None:
        if (now - self._window_start) >= self._window_seconds:
            self._slide_window()

    def _slide_window(self) -> None:
        self._window_start = time.monotonic()
        self._request_count = 0


@dataclass(slots=True)
class QueuedRequest:
    """A request waiting to be executed by the scheduler."""

    execute: Callable[[], Awaitable[Any]]  # pyright: ignore[reportExplicitAny]
    label: str = ""


class RequestScheduler:
    """Two-tier priority scheduler.

    Pregame/ingame requests are executed first and are purged on game-state
    transitions because they target endpoints that only exist for the current
    state. General requests (player loadout, MMR, store, ...) persist across
    state changes and run when the state queue is empty.
    """

    def __init__(self, requests_per_minute: int = 30) -> None:
        self._state_queue: deque[QueuedRequest] = deque()
        self._general_queue: deque[QueuedRequest] = deque()

        self._notify: asyncio.Event = asyncio.Event()
        self._running: bool = False
        self._worker_task: asyncio.Task[None] | None = None

        self._current_is_state: bool = False
        self._current_task: asyncio.Task[Any] | None = None  # pyright: ignore[reportExplicitAny]

        self._rate_limiter: RateLimiter = RateLimiter(requests_per_minute=requests_per_minute)

    # ------------------- Public API -------------------

    @property
    def state_queue_size(self) -> int:
        return len(self._state_queue)

    @property
    def general_queue_size(self) -> int:
        return len(self._general_queue)

    @property
    def rate_limiter(self) -> RateLimiter:
        return self._rate_limiter

    def start(self) -> None:
        """Start the worker loop. No-op if already running."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info(f"Request scheduler started ({self._rate_limiter.limit} req/min)")

    def stop(self) -> None:
        """Stop the worker and purge all queues."""
        self._running = False
        self._wake()
        if self._worker_task and not self._worker_task.done():
            _ = self._worker_task.cancel()
        self._worker_task = None
        self._cancel_current()
        self._state_queue.clear()
        self._general_queue.clear()
        logger.info("Request scheduler stopped")

    def on_state_change(self) -> None:
        """Handle a game-state transition.

        Purges queued state-bound requests and cancels any in-flight state
        request. The general queue is left untouched.
        """
        self._purge_state_queue()
        if self._current_is_state:
            self._cancel_current()

    def enqueue_state(
        self,
        execute: Callable[[], Awaitable[Any]],  # pyright: ignore[reportExplicitAny]
        label: str = "",
    ) -> None:
        """Add a state-bound request (high priority, purged on state change)."""
        self._state_queue.append(QueuedRequest(execute=execute, label=label))
        logger.debug(f"Enqueued state request: {label}")
        self._wake()

    def enqueue_general(
        self,
        execute: Callable[[], Awaitable[Any]],  # pyright: ignore[reportExplicitAny]
        label: str = "",
    ) -> None:
        """Add a general request (low priority, survives state changes)."""
        self._general_queue.append(QueuedRequest(execute=execute, label=label))
        logger.debug(f"Enqueued general request: {label}")
        self._wake()

    # ------------------- Internal -------------------

    def _wake(self) -> None:
        self._notify.set()

    def _cancel_current(self) -> None:
        if self._current_task and not self._current_task.done():
            _ = self._current_task.cancel()
            logger.debug("Cancelled in-flight request")

    def _try_dequeue(self) -> tuple[QueuedRequest, bool] | None:
        """Pop the next request, preferring the state queue."""
        if self._state_queue:
            return self._state_queue.popleft(), True
        if self._general_queue:
            return self._general_queue.popleft(), False
        return None

    def _purge_state_queue(self) -> None:
        count = len(self._state_queue)
        self._state_queue.clear()
        if count:
            logger.info(f"Purged {count} state-bound request(s)")

    async def _worker(self) -> None:
        logger.debug("Scheduler worker started")

        while self._running:
            result = self._try_dequeue()
            if result is None:
                self._notify.clear()
                _ = await self._notify.wait()
                continue

            item, is_state = result
            self._current_is_state = is_state
            queue_type = "state" if is_state else "general"

            try:
                await self._rate_limiter.wait_for_slot()
                logger.debug(f"Executing {queue_type} request: {item.label}")
                self._current_task = asyncio.create_task(item.execute())  # pyright: ignore[reportArgumentType]
                _ = await self._current_task  # pyright: ignore[reportAny]
            except asyncio.CancelledError:
                logger.debug(f"Request cancelled: {item.label}")
            except Exception as e:
                logger.warning(f"Request failed ({item.label}): {e}")
            finally:
                self._current_task = None
                self._current_is_state = False

        logger.debug("Scheduler worker stopped")
