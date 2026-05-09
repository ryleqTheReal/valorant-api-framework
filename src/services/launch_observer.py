"""
Monitors the Riot Client login state and Valorant process

Two independent watchers:

RiotClientWatcher
    Watches the lockfile for changes and polls '/rso-auth/v1/authorization'
    to detect login/logout.This is the earliest signal that API requests
    can begin => no need to wait for Valorant to launch anymore

    State machine:
        LOGGED_OUT (lockfile exists + RSO 200) => LOGGED_IN
                    emit(RSO_LOGIN, lockfile_data)

        LOGGED_IN (RSO non-200 / lockfile gone) => LOGGED_OUT
                   emit(RSO_LOGOUT)

ProcessWatcher
    Polls for the VALORANT process to detect game open/close
    Used by websocket and gamestate handlers that need the game running.

    State machine:
        NOT_RUNNING (process found) => RUNNING
                     emit(VALORANT_OPENED, lockfile_data)

        RUNNING (process gone) => NOT_RUNNING
                 emit(VALORANT_CLOSED)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import psutil

from utils.models import LockfileData
from utils.constants import VALORANT_PROCESS_NAMES, RIOT_CLIENT_PROCESS_NAMES
from utils.file_utils import get_default_lockfile_path
from services.event_bus import EventBus, Event

logger = logging.getLogger(__name__)

# -- Process lookup (cross-platform via psutil) --------------------------------

def find_process_by_names(names: set[str]) -> psutil.Process | None:
    """Search for a running process by a set of possible names.
       Returns the first match or `None`.

    Args:
        names (set[str]): A set of possible process names

    Returns:
        psutil.Process | None: Either returns a process instance or `None`
    """
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] in names:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def is_valorant_running() -> bool:
    return find_process_by_names(VALORANT_PROCESS_NAMES) is not None


def is_riot_client_running() -> bool:
    return find_process_by_names(RIOT_CLIENT_PROCESS_NAMES) is not None


async def read_lockfile_with_retry(
    path: Path,
    max_retries: int = 10,
    delay: float = 1.0,
) -> LockfileData | None:
    """Attempt to read the lockfile with retries.

    Args:
        path: The lockfile path
        max_retries: Maximum number of attempts
        delay: Seconds between retries

    Returns:
        LockfileData | None
    """
    for attempt in range(max_retries):
        try:
            if path.exists() and path.stat().st_size > 0:
                data = LockfileData.from_file(path)
                logger.info(f"Lockfile read successfully (attempt {attempt + 1})")
                return data
        except (ValueError, PermissionError, OSError) as e:
            logger.debug(f"Lockfile not ready yet: {e}")

        if attempt < max_retries - 1:
            await asyncio.sleep(delay)

    logger.warning(f"Lockfile unreadable after {max_retries} attempts")
    return None


class RiotClientWatcher:
    """
    Watches the Riot Client lockfile and polls '/rso-auth/v1/authorization'
    to detect when the user logs in or out

    This replaces process-based VALORANT detection as the trigger for
    authentication => API requests can begin as soon as the user is
    logged into the riot client, without VALORANT needing to be open
    """

    def __init__(
        self,
        bus: EventBus,
        poll_interval: int = 3,
        lockfile_path: Path | None = None,
    ) -> None:
        self.bus: EventBus = bus
        self.poll_interval: int = poll_interval
        self.lockfile_path: Path = lockfile_path or get_default_lockfile_path()

        self._logged_in: bool = False
        self._last_lockfile_content: str = ""
        self._lockfile: LockfileData | None = None
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None

    async def start_polling(self) -> None:
        """Start the polling loop as a background task."""
        logger.info(
            f"RiotClientWatcher started (interval: {self.poll_interval}s, lockfile: {self.lockfile_path})"
        )
        self._task = asyncio.create_task(self._poll_loop())

    async def stop_polling(self) -> None:
        """Stop the polling loop and clean up."""
        if self._task and not self._task.done():
            _ = self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._close_client()
        logger.info("RiotClientWatcher stopped")

    async def _poll_loop(self) -> None:
        """Main polling loop: check lockfile, then probe RSO endpoint"""
        
        try:
            while True:
                await self._poll_once()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            logger.debug("RiotClientWatcher poll loop cancelled")
            raise

    async def _poll_once(self) -> None:
        """Single poll iteration: read lockfile, check RSO auth state"""
        

        # Step 1: Check lockfile
        if not self.lockfile_path.exists():
            if self._logged_in:
                await self._transition_logout()
            self._lockfile = None
            self._last_lockfile_content = ""
            return

        # Step 2: Read lockfile, detect changes
        try:
            content = self.lockfile_path.read_text(encoding="utf-8").strip()
        except (PermissionError, OSError):
            return

        if content != self._last_lockfile_content:
            if self._last_lockfile_content:
                logger.info("Lockfile changed, re-reading credentials")
            self._last_lockfile_content = content

            try:
                self._lockfile = LockfileData.from_file(self.lockfile_path)
            except (ValueError, OSError) as e:
                logger.debug(f"Lockfile not ready: {e}")
                return

            # Lockfile changed => rebuild the httpx client
            await self._close_client()
            self._client = httpx.AsyncClient(verify=False, timeout=5)

            # If we were logged in with old credentials, force re-check
            if self._logged_in:
                await self._transition_logout()

        if not self._lockfile or not self._client:
            return

        # Step 3: Probe /rso-auth/v1/authorization
        try:
            response = await self._client.get(
                f"{self._lockfile.base_url}/rso-auth/v1/authorization",
                headers={"Authorization": self._lockfile.auth_header},
            )
        except (httpx.ConnectError, httpx.ReadError, httpx.WriteError):
            # Client is shutting down or not ready
            if self._logged_in:
                await self._transition_logout()
            return
        except httpx.HTTPError:
            return

        if response.status_code == 200 and not self._logged_in:
            await self._transition_login()
        elif response.status_code != 200 and self._logged_in:
            await self._transition_logout()

    async def _transition_login(self) -> None:
        """User just logged in => emit RSO_LOGIN with lockfile data"""
        self._logged_in = True
        logger.info("RSO login detected")
        _ = await self.bus.emit(Event.RSO_LOGIN, self._lockfile)

    async def _transition_logout(self) -> None:
        """User just logged out => emit RSO_LOGOUT"""
        
        self._logged_in = False
        logger.info("RSO logout detected")
        _ = await self.bus.emit(Event.RSO_LOGOUT)

    async def _close_client(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


class ProcessWatcher:
    """
    Polls for the VALORANT process to detect game open/close.+

    Used by websocket and gamestate handlers that need the actual
    game running (presence data, in-game state tracking)
    """

    def __init__(
        self,
        bus: EventBus,
        poll_interval: int = 3,
        lockfile_path: Path | None = None,
    ) -> None:
        self.bus: EventBus = bus
        self.poll_interval: int = poll_interval
        self.lockfile_path: Path = lockfile_path or get_default_lockfile_path()

        self._valorant_was_running: bool = False
        self._task: asyncio.Task[None] | None = None

    async def start_polling(self) -> None:
        """Start the polling loop as a background task"""
        logger.info(
            f"ProcessWatcher started (interval: {self.poll_interval}s)"
        )
        self._task = asyncio.create_task(self._poll_loop())

    async def stop_polling(self) -> None:
        """Stop the polling loop"""
        if self._task and not self._task.done():
            _ = self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("ProcessWatcher stopped")

    async def _poll_loop(self) -> None:
        """Check process status at regular intervals"""
        try:
            while True:
                valorant_running: bool = is_valorant_running()

                # transition: not running -> running
                if valorant_running and not self._valorant_was_running:
                    logger.info("VALORANT process detected!")

                    lockfile_data = await read_lockfile_with_retry(
                        self.lockfile_path
                    )

                    if lockfile_data:
                        _ = await self.bus.emit(Event.VALORANT_OPENED, lockfile_data)
                    else:
                        logger.warning("VALORANT is running but lockfile is unreadable. Skipping.")

                # transition: running -> not running
                elif not valorant_running and self._valorant_was_running:
                    logger.info("VALORANT process terminated")
                    _ = await self.bus.emit(Event.VALORANT_CLOSED)

                self._valorant_was_running = valorant_running
                await asyncio.sleep(self.poll_interval)

        except asyncio.CancelledError:
            logger.debug("ProcessWatcher poll loop cancelled")
            raise