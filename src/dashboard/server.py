import os
import sys
import json
import time
import signal
import subprocess
from typing import Dict, Any, Optional
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from ..config.settings import load_config, BotConfig
from ..storage.state_store import StateStore
from ..providers.opensea.client import OpenSeaClient
from ..providers.opensea.provider import OpenSeaProvider
from ..collectors.orchestrator import CollectionEvaluator
from ..utils.logging import setup_logger

logger = setup_logger("dashboard_server")

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
PID_FILE = os.path.join(WORKSPACE_ROOT, "state", "bot.pid")
LOG_FILE = os.path.join(WORKSPACE_ROOT, "bot.log")

class BotProcessManager:
    """Manages spawning, PID tracking, and graceful termination of the bot daemon."""

    @staticmethod
    def get_running_pid() -> Optional[int]:
        if not os.path.exists(PID_FILE):
            return None
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            # Check if process is actually running
            os.kill(pid, 0)
            return pid
        except (ValueError, OSError):
            # Process does not exist or stale PID file
            if os.path.exists(PID_FILE):
                try:
                    os.remove(PID_FILE)
                except OSError:
                    pass
            return None

    @staticmethod
    def start_bot(config_path: str = "config/config.yaml", dry_run: bool = False) -> Dict[str, Any]:
        current_pid = BotProcessManager.get_running_pid()
        if current_pid is not None:
            return {"success": False, "error": f"Bot is already running (PID {current_pid})", "pid": current_pid}

        cmd = [sys.executable, "bot.py", "--config", config_path]
        if dry_run:
            cmd.append("--dry-run")

        log_fp = open(LOG_FILE, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=WORKSPACE_ROOT,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            with open(PID_FILE, "w") as f:
                f.write(str(proc.pid))
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

            # Wait up to 5 seconds for graceful shutdown
            for _ in range(50):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    # Process exited
                    break
            else:
                # Force kill if still alive
                logger.warning("Bot PID %d did not terminate on SIGINT. Sending SIGTERM...", pid)
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)

            if os.path.exists(PID_FILE):
                try:
                    os.remove(PID_FILE)
                except OSError:
                    pass

            return {"success": True, "status": "STOPPED", "pid": pid}
        except Exception as e:
            logger.error("Error stopping bot PID %d: %s", pid, e)
            return {"success": False, "error": str(e)}

class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler providing REST endpoints and serving static UI assets."""

    config: BotConfig
    state_store: StateStore
    evaluator: CollectionEvaluator

    def log_message(self, format, *args):
        # Suppress verbose standard HTTP server logging in production
        return

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str = "text/plain", status: int = 200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, filepath: str, content_type: str):
        if not os.path.exists(filepath):
            self._send_text("Not Found", status=404)
            return
        with open(filepath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # -------------------------------------------------------------
        # API Routes
        # -------------------------------------------------------------
        if path == "/api/status":
            self._handle_get_status()
        elif path == "/api/collections":
            self._handle_get_collections(query)
        elif path == "/api/candidates":
            self._handle_get_candidates()
        elif path == "/api/candidate/dossier":
            self._handle_get_candidate_dossier(query)
        elif path == "/api/inspect":
            self._handle_inspect_collection(query)
        elif path == "/api/logs":
            self._handle_get_logs(query)
        # -------------------------------------------------------------
        # Static Assets
        # -------------------------------------------------------------
        elif path in ("/", "/index.html"):
            self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        elif path == "/static/app.css":
            self._send_file(os.path.join(STATIC_DIR, "app.css"), "text/css; charset=utf-8")
        elif path == "/static/app.js":
            self._send_file(os.path.join(STATIC_DIR, "app.js"), "application/javascript; charset=utf-8")
        else:
            self._send_text("Not Found", status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/bot/start":
            self._handle_start_bot()
        elif path == "/api/bot/stop":
            self._handle_stop_bot()
        else:
            self._send_text("Not Found", status=404)

    def _handle_get_status(self):
        pid = BotProcessManager.get_running_pid()
        summary = self.state_store.get_status_summary()

        uptime_seconds = None
        if pid and os.path.exists(PID_FILE):
            try:
                uptime_seconds = int(time.time() - os.path.getmtime(PID_FILE))
            except Exception:
                pass

        data = {
            "is_running": pid is not None,
            "status": "RUNNING" if pid else "STOPPED",
            "pid": pid,
            "uptime_seconds": uptime_seconds,
            "total_candidates": summary.get("total_candidates_found", 0),
            "total_monitored": summary.get("total_monitored_collections", 0),
            "checkpoints": summary.get("checkpoints", {}),
            "telemetry": summary.get("telemetry", {}),
            "bot_timezone": self.config.general.bot_timezone,
        }
        self._send_json(data)

    def _handle_start_bot(self):
        content_length = int(self.headers.get("Content-Length", 0))
        dry_run = False
        if content_length > 0:
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                dry_run = bool(payload.get("dry_run", False))
            except Exception:
                pass

        result = BotProcessManager.start_bot(dry_run=dry_run)
        self._send_json(result, status=200 if result.get("success") else 400)

    def _handle_stop_bot(self):
        result = BotProcessManager.stop_bot()
        self._send_json(result, status=200 if result.get("success") else 500)

    def _handle_get_collections(self, query):
        search = query.get("q", [None])[0]
        limit = int(query.get("limit", [100])[0])
        collections = self.state_store.get_all_monitored_collections(search=search, limit=limit)
        self._send_json({"collections": collections, "count": len(collections)})

    def _handle_get_candidates(self):
        """Scans the filesystem data directory and merges with candidate history."""
        data_root = os.path.abspath(self.config.general.data_root)
        candidates = []

        if os.path.exists(data_root):
            for day in sorted(os.listdir(data_root), reverse=True):
                day_path = os.path.join(data_root, day)
                if not os.path.isdir(day_path) or day.startswith("."):
                    continue
                for proj in sorted(os.listdir(day_path)):
                    proj_path = os.path.join(day_path, proj)
                    info_path = os.path.join(proj_path, "Info.md")
                    if os.path.isdir(proj_path) and os.path.exists(info_path):
                        candidates.append({
                            "date": day,
                            "project_name": proj.replace("-", " "),
                            "folder_name": proj,
                            "info_path": info_path,
                            "file_size": os.path.getsize(info_path),
                            "modified_at": os.path.getmtime(info_path),
                        })

        history = self.state_store.get_all_candidates_history(limit=50)
        self._send_json({
            "candidates": candidates,
            "count": len(candidates),
            "history": history,
        })

    def _handle_get_candidate_dossier(self, query):
        date_str = query.get("date", [None])[0]
        folder_name = query.get("project", [None])[0]

        if not date_str or not folder_name:
            self._send_json({"error": "Missing 'date' or 'project' query parameters"}, status=400)
            return

        # Security check: prevent directory traversal
        if ".." in date_str or ".." in folder_name or "/" in date_str or "/" in folder_name:
            self._send_json({"error": "Invalid file path"}, status=400)
            return

        info_path = os.path.join(self.config.general.data_root, date_str, folder_name, "Info.md")
        if not os.path.exists(info_path):
            self._send_json({"error": "Dossier not found"}, status=404)
            return

        with open(info_path, "r", encoding="utf-8") as f:
            content = f.read()

        self._send_json({
            "date": date_str,
            "project": folder_name,
            "content": content,
        })

    def _handle_inspect_collection(self, query):
        slug = query.get("slug", [None])[0]
        if not slug:
            self._send_json({"error": "Missing 'slug' parameter"}, status=400)
            return

        slug = slug.strip().lower()
        if "opensea.io/collection/" in slug:
            slug = slug.split("opensea.io/collection/")[-1].strip("/?# ")

        try:
            report = self.evaluator.evaluate_collection(slug=slug, dry_run=True, stop_on_first_failure=False)
            if not report:
                self._send_json({
                    "slug": slug,
                    "evaluated": False,
                    "error": f"Could not retrieve metadata for collection '{slug}' from OpenSea.",
                }, status=404)
                return

            criteria_data = {}
            for name, crit in report.criteria.items():
                criteria_data[name] = {
                    "name": name,
                    "threshold": crit.threshold,
                    "actual_value": str(crit.actual_value),
                    "unit": crit.unit,
                    "result": crit.result.value,
                    "formula": crit.formula,
                    "notes": crit.notes,
                    "data_quality": crit.data_quality.value,
                }

            self._send_json({
                "slug": slug,
                "evaluated": True,
                "is_overall_pass": report.is_overall_pass,
                "rejection_reasons": report.rejection_reasons,
                "criteria": criteria_data,
            })
        except Exception as e:
            logger.error("Error inspecting collection %s: %s", slug, e)
            self._send_json({"slug": slug, "evaluated": False, "error": str(e)}, status=500)

    def _handle_get_logs(self, query):
        max_lines = int(query.get("lines", [150])[0])
        if not os.path.exists(LOG_FILE):
            self._send_json({"lines": ["No logs yet. Start the bot to begin logging."]})
            return

        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail_lines = [l.rstrip("\r\n") for l in lines[-max_lines:]]
            self._send_json({"lines": tail_lines, "total_lines": len(lines)})
        except Exception as e:
            self._send_json({"lines": [f"Error reading logs: {e}"]})

def run_dashboard_server(host: str = "127.0.0.1", port: int = 5050, config_path: str = "config/config.yaml"):
    """Starts the dashboard HTTP server."""
    cfg = load_config(config_path)
    store = StateStore(cfg.general.state_db_path)
    client = OpenSeaClient(api_key=cfg.opensea_api_key)
    provider = OpenSeaProvider(client)
    evaluator = CollectionEvaluator(provider, store, cfg)

    # Attach to request handler class
    DashboardRequestHandler.config = cfg
    DashboardRequestHandler.state_store = store
    DashboardRequestHandler.evaluator = evaluator

    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, DashboardRequestHandler)
    print(f"\n=======================================================")
    print(f"🚀 NFT Bot Dashboard running at: http://{host}:{port}")
    print(f"=======================================================\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        httpd.server_close()
