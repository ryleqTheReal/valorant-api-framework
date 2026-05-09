"""Singleton configuration manager.

Usage::

    from src.services.config_manager import ConfigManager

    cfg = ConfigManager()        # first call loads config.json
    print(cfg.config.server_base_url)

    cfg.reload()                 # re-read from disk at runtime
"""

# Only for type annotation fix
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import final

from utils.models import AppConfig
from utils.file_utils import get_config_path


@final # Makes sure that this class cannot be used as subclass, makes my analyzer not complain
class ConfigManager:
    """Thread-safe, lazy-loading singleton that exposes an ``AppConfig``."""

    _instance: ConfigManager | None = None
    _lock = Lock()
    _path: Path | None = None
    _config: AppConfig | None = None

    def __new__(cls, path: Path | None = None) -> ConfigManager:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._path = get_config_path()
                inst._config = None
                cls._instance = inst
        return cls._instance

    @property
    def config(self) -> AppConfig:
        """Return the current ``AppConfig``, loading on first access."""

        if self._config is None:
            return self._load()
        return self._config

    def reload(self) -> AppConfig:
        """Re-read config.json from disk and return the updated config."""
        return self._load()


    def _load(self) -> AppConfig:
        with self._lock:
            if not self._path:
                self._path = get_config_path()
            raw = json.loads(self._path.read_text(encoding="utf-8"))    # pyright: ignore[reportAny]
            self._config = AppConfig.from_config_dict(raw)  # pyright: ignore[reportAny]
            return self._config



    @classmethod
    def _reset(cls) -> None:  # pyright: ignore[reportUnusedFunction]
        """Destroy the singleton (for testing only)."""
        with cls._lock:
            cls._instance = None
