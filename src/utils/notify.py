import subprocess
import sys

from .logging import setup_logger

logger = setup_logger("notify")


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mac_notification(title: str, message: str, sound: bool = False) -> bool:
    """Shows a macOS notification banner. Does nothing on other systems. Never raises."""
    if sys.platform != "darwin":
        return False
    script = f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
    if sound:
        script += ' sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:  # notification is best effort
        logger.debug("Notification failed: %s", e)
        return False
