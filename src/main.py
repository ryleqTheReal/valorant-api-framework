"""
Entry point.

Wires up the event bus, the Riot Client / process watchers, the auth
handler, the local websocket, and the gamestate tracker. Then runs in
the background until SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from services.event_bus import EventBus, Event
from services.launch_observer import RiotClientWatcher, ProcessWatcher
from services.auth_service import AuthHandler
from services.gamesocket import GameSocketHandler
from services.gamestates import GamestateHandler
from services.config_manager import ConfigManager
from services.request_scheduler import RequestScheduler

from utils.file_utils import get_config_path


_log_format = "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s"
_log_datefmt = "%H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=_log_format,
    datefmt=_log_datefmt,
)

# Mirror warnings and errors to data/errors.log for offline review
_error_log_path = Path(__file__).resolve().parents[1] / "data" / "errors.log"
_error_log_path.parent.mkdir(parents=True, exist_ok=True)
_file_handler = logging.FileHandler(_error_log_path, encoding="utf-8")
_file_handler.setLevel(logging.WARNING)
_file_handler.setFormatter(logging.Formatter(_log_format, datefmt=_log_datefmt))
logging.getLogger().addHandler(_file_handler)

logger: logging.Logger = logging.getLogger("main")


class App:
    """Orchestrates all components."""

    def __init__(self) -> None:
        self.bus: EventBus = EventBus()
        self.cfg: ConfigManager = ConfigManager(get_config_path())
        self.scheduler: RequestScheduler = RequestScheduler(
            requests_per_minute=self.cfg.config.rate_limit_per_minute,
        )
        self.riot_watcher: RiotClientWatcher = RiotClientWatcher(self.bus, self.cfg.config.poll_interval)
        self.process_watcher: ProcessWatcher = ProcessWatcher(self.bus, self.cfg.config.poll_interval)
        self.auth: AuthHandler = AuthHandler(self.bus)
        self.gamesocket: GameSocketHandler = GameSocketHandler(self.bus)
        self.gamestates: GamestateHandler = GamestateHandler(self.bus, self.scheduler)

    async def run(self) -> None:
        """Start the app and block until a shutdown signal is received."""
        logger.info("=" * 60)
        logger.info("  Valorant API event loop started")
        logger.info(f"  Rate limit:  {self.cfg.config.rate_limit_per_minute} req/min")
        logger.info(f"  Poll every:  {self.cfg.config.poll_interval}s")
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

        _ = await self.riot_watcher.start_polling()
        _ = await self.process_watcher.start_polling()
        _ = await shutdown_event.wait()

        logger.info("Shutting down ...")
        _ = await self.bus.emit(Event.SHUTDOWN)
        _ = await self.riot_watcher.stop_polling()
        _ = await self.process_watcher.stop_polling()

        logger.info("App terminated.")


def main() -> None:
    while True:
        try:
            app = App()
            asyncio.run(app.run())
            break
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception("Crashed unexpectedly, restarting...")


if __name__ == "__main__":
    main()
