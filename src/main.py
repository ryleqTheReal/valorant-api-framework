"""
Starts the event bus, process watcher, and all handlers,
then runs in the background until SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from services.event_bus import EventBus, Event
from services.launch_observer import ProcessWatcher
from services.auth_service import AuthHandler
from services.gamesocket import GameSocketHandler
from services.gamestates import GamestateHandler
from services.config_manager import ConfigManager

from utils.file_utils import get_config_path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
    datefmt="%H:%M:%S",
)
logger: logging.Logger = logging.getLogger("main")

class ValorantStatsApp:
    """Orchestrates all components."""

    def __init__(self) -> None:
        self.bus: EventBus = EventBus()
        self.cfg: ConfigManager = ConfigManager(get_config_path())
        self.watcher: ProcessWatcher = ProcessWatcher(self.bus, self.cfg.config.poll_interval)
        self.auth: AuthHandler = AuthHandler(self.bus)
        self.gamesocket: GameSocketHandler = GameSocketHandler(self.bus)
        self.gamestates: GamestateHandler = GamestateHandler(self.bus)

    async def run(self) -> None:
        """Start the app and block until a shutdown signal is received."""
        
        logger.info("=" * 60)
        logger.info("  Valorant Stats Collector started")
        logger.info("=" * 60)

        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _signal_handler() -> None:
            logger.info("Shutdown signal received")
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows does not support add_signal_handler
                _ = signal.signal(sig, lambda s, f: shutdown_event.set())

        _ = await self.watcher.start_polling()
        _ = await shutdown_event.wait()

        logger.info("Shutting down ...")
        _ = await self.bus.emit(Event.SHUTDOWN)
        _ = await self.watcher.stop_polling()
        logger.info("App terminated.")


def main() -> None:
    while True:
        try:
            app = ValorantStatsApp()
            asyncio.run(app.run())
            break
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception("Crashed unexpectedly, restarting...")


if __name__ == "__main__":
    main()