"""
Keeps the bot running across power cuts and crashes.

- The desired run state ("should the bot be on?") is saved in state/desired_state.json whenever
  you press Start or Stop, so after a restart the bot comes back exactly as you left it.
- The dashboard (started at login by launchd, see launchd.py) supervises the bot process and
  restarts it when it should be on but is not.
- PID checks confirm the process really is the bot, so a stale PID file left by a power cut
  (whose number the system has since given to another program) is ignored.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..utils.logging import setup_logger

logger = setup_logger("runtime_control")

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATE_DIR = os.path.join(WORKSPACE_ROOT, "state")
PID_FILE = os.path.join(STATE_DIR, "bot.pid")
DESIRED_STATE_FILE = os.path.join(STATE_DIR, "desired_state.json")
LOG_FILE = os.path.join(WORKSPACE_ROOT, "bot.log")

# Arguments that mean "this bot.py process is not the monitoring daemon"
NON_DAEMON_COMMANDS = {"ui", "dashboard", "inspect", "status", "install-autostart", "uninstall-autostart"}


def atomic_write(path: str, text: str) -> None:
    """Write-then-rename with fsync, so a power cut never leaves a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ----------------------------------------------------------------------
# Desired run state
# ----------------------------------------------------------------------
def read_desired_state() -> Dict[str, Any]:
    try:
        with open(DESIRED_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {"run": bool(data.get("run")), "dry_run": bool(data.get("dry_run")), "updated_at": data.get("updated_at")}
    except (OSError, ValueError):
        return {"run": False, "dry_run": False, "updated_at": None}


def write_desired_state(run: bool, dry_run: bool = False) -> None:
    atomic_write(DESIRED_STATE_FILE, json.dumps({
        "run": run, "dry_run": dry_run, "updated_at": datetime.now(timezone.utc).isoformat(),
    }))


# ----------------------------------------------------------------------
# PID tracking
# ----------------------------------------------------------------------
def _process_command(pid: int) -> Optional[str]:
    """Command line of a running process, or None if unknown (non-POSIX or ps unavailable)."""
    if os.name != "posix":
        return None
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or ""
    except Exception:
        return None


def is_bot_process(pid: int) -> bool:
    """True if pid is alive and (where checkable) is the bot daemon, not some reused PID."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    cmd = _process_command(pid)
    if cmd is None:  # cannot check; trust the liveness test
        return True
    tokens = cmd.split()
    return any(t.endswith("bot.py") or t == "bot" for t in tokens) and not (NON_DAEMON_COMMANDS & set(tokens))


def write_pid_file(pid: int) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PID_FILE, "w") as f:  # a torn PID file is harmless: get_running_pid() treats it as stale
        f.write(str(pid))


def remove_pid_file(only_if_pid: Optional[int] = None) -> None:
    try:
        if only_if_pid is not None:
            with open(PID_FILE, "r") as f:
                if f.read().strip() != str(only_if_pid):
                    return
        os.remove(PID_FILE)
    except (OSError, ValueError):
        pass


class BotProcessManager:
    """Manages spawning, PID tracking, and graceful termination of the bot daemon."""

    @staticmethod
    def get_running_pid() -> Optional[int]:
        if not os.path.exists(PID_FILE):
            return None
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
        except (ValueError, OSError):
            remove_pid_file()
            return None
        if is_bot_process(pid):
            return pid
        # Stale PID file (e.g. left behind by a power cut)
        remove_pid_file()
        return None

    @staticmethod
    def start_bot(config_path: str = "config/config.yaml", dry_run: bool = False) -> Dict[str, Any]:
        current_pid = BotProcessManager.get_running_pid()
        if current_pid is not None:
            return {"success": False, "error": f"Bot is already running (PID {current_pid})", "pid": current_pid}

        cmd = [sys.executable, os.path.join(WORKSPACE_ROOT, "bot.py"), "--config", config_path]
        if dry_run:
            cmd.append("--dry-run")

        try:
            log_fp = open(LOG_FILE, "a", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                cwd=WORKSPACE_ROOT,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            write_pid_file(proc.pid)
            logger.info("Started bot daemon with PID %d", proc.pid)
            return {"success": True, "pid": proc.pid, "dry_run": dry_run}
        except Exception as e:
            logger.error("Failed to start bot daemon: %s", e)
            return {"success": False, "error": str(e)}

    @staticmethod
    def stop_bot() -> Dict[str, Any]:
        pid = BotProcessManager.get_running_pid()
        if pid is None:
            return {"success": True, "message": "Bot is not running", "status": "STOPPED"}

        try:
            logger.info("Sending SIGINT to bot process PID %d", pid)
            os.kill(pid, signal.SIGINT)

            # The bot finishes the collection it is on; give it up to 20 seconds
            for _ in range(200):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            else:
                logger.warning("Bot PID %d did not stop on SIGINT. Sending SIGTERM...", pid)
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        break
                else:
                    logger.warning("Bot PID %d still running. Sending SIGKILL.", pid)
                    os.kill(pid, signal.SIGKILL)

            remove_pid_file()
            return {"success": True, "status": "STOPPED", "pid": pid}
        except Exception as e:
            logger.error("Error stopping bot PID %d: %s", pid, e)
            return {"success": False, "error": str(e)}


# ----------------------------------------------------------------------
# Connectivity
# ----------------------------------------------------------------------
def is_online(host: str = "api.opensea.io", port: int = 443, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ----------------------------------------------------------------------
# Supervisor (runs inside the dashboard process)
# ----------------------------------------------------------------------
class Supervisor:
    """
    Every few seconds: if the bot should be running but is not, start it.
    Backs off after repeated crashes so a broken setup does not restart in a tight loop.
    """

    def __init__(self, config_path: str, state_store=None, check_seconds: float = 10.0):
        self.config_path = config_path
        self.state_store = state_store
        self.check_seconds = check_seconds
        self._restarts: List[float] = []
        self._first_check = True
        self._paused_until = 0.0

    def _event(self, kind: str, message: str) -> None:
        logger.info(message)
        if self.state_store is not None:
            try:
                self.state_store.log_event(kind, message)
            except Exception:
                pass

    def check_once(self) -> Optional[str]:
        desired = read_desired_state()
        if not desired["run"] or BotProcessManager.get_running_pid() is not None:
            self._first_check = False
            return None
        now = time.time()
        if now < self._paused_until:
            return None
        self._restarts = [t for t in self._restarts if now - t < 600]
        if len(self._restarts) >= 3:
            self._paused_until = now + 600
            self._restarts = []
            self._event("error", "The bot stopped 3 times in 10 minutes. Waiting 10 minutes before trying again. Check bot.log.")
            return "backoff"
        result = BotProcessManager.start_bot(config_path=self.config_path, dry_run=desired["dry_run"])
        if result.get("success"):
            self._restarts.append(now)
            if self._first_check:
                self._event("restart", "Bot started by itself after the iMac or the dashboard restarted.")
            else:
                self._event("restart", "Bot had stopped unexpectedly and was restarted.")
        self._first_check = False
        return "started" if result.get("success") else "failed"

    def run_forever(self) -> None:
        while True:
            try:
                self.check_once()
            except Exception as e:
                logger.error("Supervisor check failed: %s", e)
            time.sleep(self.check_seconds)
