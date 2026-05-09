from functools import cache
from pathlib import Path
import platform
import logging

from utils.exceptions import PathResolutionError, UnknownPlatformError

logger: logging.Logger = logging.getLogger(__name__)

"""Here we dump all low-level path resolution functinons and anything regarding paths"""

def get_default_lockfile_path() -> Path:
    """Return the platform-dependent default path to the lockfile

    Example:
        path = get_default_lockfile_path()
        lock = LockfileData.from_file(path)
    
    Returns:
        Path: The `Path` object to the lockfile
    """
    
    system = platform.system()
    if system == "Windows":
        # TESTED and confirmed
        return (
            Path.home()
            / "AppData" / "Local"
            / "Riot Games" / "Riot Client" / "Config" / "lockfile"
        )
    elif system == "Darwin":
        # UNTESTED!
        return (
            Path.home()
            / "Library" / "Application Support"
            / "Riot Games" / "Riot Client" / "Config" / "lockfile"
        )
    raise UnknownPlatformError(f"Unknown platform: '{system}'. This is unfixable.")
        
@cache
def get_recent_log_path() -> Path:
    system = platform.system()
    # os.path.join(os.getenv('LOCALAPPDATA'), R'VALORANT\Saved\Logs\ShooterGame.log')
    if system == "Windows":
        return (
            Path.home()
            / "AppData" / "Local"
            / "VALORANT" / "Saved" / "Logs" / "ShooterGame.log"
        )
    elif system == "Darwin":
        return (
            Path.home()
            / "Library" / "Application Support"
            / "VALORANT" / "Saved" / "Logs" / "ShooterGame.log"
        )
    raise UnknownPlatformError(f"Unknown platform: '{system}'. This is unfixable.")
    
@cache  
def get_config_path() -> Path:
    """Returns a Path object to the app config json

    Raises:
        FileNotFoundError: The config file does not exist

    Returns:
        Path: The path object to the config file
    """
    config_path: Path = Path(__file__).resolve().parents[2] / "config.json"
    if not config_path.exists():
        logger.error("The config file does not exist, cannot launch properly.")
        raise PathResolutionError("The config file does not exist, cannot launch properly.")
    return config_path