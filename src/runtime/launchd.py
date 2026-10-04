"""
macOS auto-start: a LaunchAgent that opens the dashboard at login and reopens it if it quits.
The dashboard then starts the bot if it was on before (see control.Supervisor).

Combined with the iMac settings "Start up automatically after a power failure" and automatic
login, the bot comes back by itself after a power cut. See docs/MAC_SETUP.md.
"""
import os
import plistlib
import subprocess
import sys
from typing import Dict, Any

from .control import WORKSPACE_ROOT

LABEL = "com.nftmonitor.dashboard"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def build_plist(config_path: str, port: int = 5050) -> Dict[str, Any]:
    logs = os.path.join(WORKSPACE_ROOT, "logs")
    return {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable, os.path.join(WORKSPACE_ROOT, "bot.py"),
            "--config", os.path.abspath(config_path), "ui", "--port", str(port),
        ],
        "WorkingDirectory": WORKSPACE_ROOT,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": os.path.join(logs, "dashboard.log"),
        "StandardErrorPath": os.path.join(logs, "dashboard.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def is_installed() -> bool:
    return os.path.exists(PLIST_PATH)


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(config_path: str, port: int = 5050) -> str:
    if sys.platform != "darwin":
        return "Auto-start uses macOS launchd, so it only works on the Mac."
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    os.makedirs(os.path.join(WORKSPACE_ROOT, "logs"), exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(build_plist(config_path, port), f)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, PLIST_PATH)  # reload if it was already there
    res = _launchctl("bootstrap", domain, PLIST_PATH)
    if res.returncode != 0:
        res = _launchctl("load", "-w", PLIST_PATH)
    if res.returncode != 0:
        return f"Wrote {PLIST_PATH}, but launchctl failed: {res.stderr.strip()}"
    return (f"Auto-start is on. The dashboard opens at login on http://127.0.0.1:{port} "
            f"and starts the bot if it was running before.")


def uninstall() -> str:
    if sys.platform != "darwin":
        return "Auto-start uses macOS launchd, so it only works on the Mac."
    if os.path.exists(PLIST_PATH):
        res = _launchctl("bootout", f"gui/{os.getuid()}", PLIST_PATH)
        if res.returncode != 0:
            _launchctl("unload", "-w", PLIST_PATH)
        os.remove(PLIST_PATH)
    return "Auto-start is off. The dashboard and bot no longer start at login."
