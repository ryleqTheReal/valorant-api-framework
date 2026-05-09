"""
Monitors whether the VALORANT process is running.

More reliable than lockfile-watching because the lockfile persists
even when only the Riot Client (without Valorant) is open.

Flow:
    Process detected  -> read lockfile -> start AUTH
    Process gone      -> end session
"""

# For dynamic type definitions
from __future__ import annotations 

import asyncio
import logging
from pathlib import Path

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
       Note: This should hopefully work cross-platform
       
    Example:
        from src/utils/constants.py import VALORANT_PROCESS_NAMES
        find_process_by_name(VALORANT_PROCESS_NAMES)

    Args:
        names (set[str]): A set of possible app names for VALORANT

    Returns:
        psutil.Process | None: Either returns a process instance or `None`
    """
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] in names:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # Process terminated between iteration and access,
            # or we lack permissions
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
    """Attempt to read the lockfile.
    Retries with a delay because the file may still be empty
    or locked shortly after the process starts.

    Args:
        path (Path): The path to the file obtained by file_utils -> get_default_lockfile_path
        max_retries (int, optional): Defaults to 10.
        delay (float, optional): Defaults to 1.0.

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

class ProcessWatcher:
    """
    Polls periodically to check if the VALORANT process is running.

    Why polling instead of filesystem events?
    - The lockfile is NOT reliable (it exists even without Valorant)
    - Native process events (e.g. ETW on Windows) are platform-specific
    - psutil polling every few seconds is simple, reliable,
      and works the same everywhere

    State machine:
        NOT_RUNNING --(process found)--> RUNNING
                     emit(VALORANT_OPENED)

        RUNNING --(process gone)--> NOT_RUNNING
                 emit(VALORANT_CLOSED)
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
            f"ProcessWatcher started (interval: {self.poll_interval}s, lockfile: {self.lockfile_path})"
        )
        self._task = asyncio.create_task(self._poll_loop())

    async def stop_polling(self) -> None:
        """Stop the polling loop"""
        
        if self._task and not self._task.done():
            # only using _ because type analysis tool is complaining...
            _ = self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("ProcessWatcher stopped")

    async def _poll_loop(self) -> None:
        """Checks process status at regular intervals."""
        
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
                        # again using _ to avoid analyzer complaining
                        _ = await self.bus.emit(Event.VALORANT_OPENED, lockfile_data)
                    else:
                        logger.warning("VALORANT is running but lockfile is unreadable. Skipping auth.")

                # transition: running -> not running
                elif not valorant_running and self._valorant_was_running:
                    logger.info("VALORANT process terminated")
                    _ = await self.bus.emit(Event.VALORANT_CLOSED)

                self._valorant_was_running = valorant_running
                await asyncio.sleep(self.poll_interval)

        except asyncio.CancelledError:
            logger.debug("Poll loop cancelled")
            raise