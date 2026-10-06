import os
import json
import threading
from datetime import timedelta
from typing import Dict, Any, Optional, List
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import yaml

from ..config.settings import load_config, BotConfig, overrides_path_for, deep_merge, reload_env
from ..config.chains import CHAINS, ALL_CHAIN_IDS
from ..storage.state_store import StateStore
from ..providers.opensea.client import OpenSeaClient, OpenSeaNetworkError
from ..providers.opensea.provider import OpenSeaProvider
from ..collectors.orchestrator import CollectionEvaluator
from ..runtime import control, launchd
from ..runtime.control import BotProcessManager, PID_FILE, LOG_FILE, WORKSPACE_ROOT  # noqa: F401 (re-exported)
from ..utils.logging import setup_logger
from ..utils.net import use_ipv4_only
from ..utils.prices import PriceBook, WRAPPED_TO_NATIVE
from ..utils.time import now_local

logger = setup_logger("dashboard_server")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
}

# Rule key -> (config attribute holding the limit, or None for yes/no rules)
RULE_LIMIT_FIELDS = {
    "verification": None,
    "project_age": "min_age_days",
    "listed_items": "max_listed_pct",
    "trading_frequency": "max_threshold",
    "floor_change_1d": "max_change_pct",
    "floor_change_7d": "max_change_pct",
    "offer_to_floor": "min_ratio_pct",
    "net_profit": "min_net_roi_pct",
}


def _telemetry_value(telemetry: Dict[str, Any], key: str) -> Any:
    raw = (telemetry.get(key) or {}).get("value")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _leaf_paths(d: Dict[str, Any], prefix=()) -> List[tuple]:
    out = []
    for k, v in d.items():
        p = prefix + (k,)
        if isinstance(v, dict) and k != "chain_gas":
            out.extend(_leaf_paths(v, p))
        else:
            out.append(p)
    return out


def _delete_path(d: Dict[str, Any], keys: tuple) -> None:
    for k in keys[:-1]:
        d = d.get(k)
        if not isinstance(d, dict):
            return
    d.pop(keys[-1], None)


def _prune_same(new: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """Keeps only values that differ from base. chain_gas is compared per chain."""
    out: Dict[str, Any] = {}
    for k, v in new.items():
        b = base.get(k) if isinstance(base, dict) else None
        if isinstance(v, dict):
            sub = _prune_same(v, b if isinstance(b, dict) else {})
            if sub:
                out[k] = sub
        elif isinstance(v, float) and isinstance(b, (int, float)) and abs(v - b) < 1e-12:
            continue
        elif v != b:
            out[k] = v
    return out


def _drop_empty(d: Dict[str, Any]) -> None:
    for k in list(d.keys()):
        if isinstance(d[k], dict):
            _drop_empty(d[k])
            if not d[k]:
                del d[k]


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler providing REST endpoints and serving static UI assets."""

    config: BotConfig
    state_store: StateStore
    evaluator: CollectionEvaluator
    config_path: str = "config/config.yaml"
    price_book: Optional[PriceBook] = None

    def log_message(self, format, *args):
        # Suppress verbose standard HTTP server logging in production
        return

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _today(self) -> str:
        return now_local(self.config.general.bot_timezone).strftime("%Y-%m-%d")

    def _days_ago(self, n: int) -> str:
        return (now_local(self.config.general.bot_timezone).date() - timedelta(days=n)).strftime("%Y-%m-%d")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        routes = {
            "/api/status": lambda: self._handle_get_status(),
            "/api/overview": lambda: self._handle_get_overview(),
            "/api/collections": lambda: self._handle_get_collections(query),
            "/api/candidates": lambda: self._handle_get_candidates(query),
            "/api/candidate": lambda: self._handle_get_candidate(query),
            "/api/candidate/dossier": lambda: self._handle_get_candidate_dossier(query),
            "/api/near-misses": lambda: self._handle_get_near_misses(),
            "/api/inspect": lambda: self._handle_inspect_collection(query),
            "/api/settings": lambda: self._handle_get_settings(),
            "/api/logs": lambda: self._handle_get_logs(query),
            "/api/prices": lambda: self._handle_get_prices(),
        }
        if path in routes:
            routes[path]()
        elif path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            self._send_file(os.path.join(STATIC_DIR, name), ctype)
        else:
            self._send_text("Not Found", status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/bot/start":
            self._handle_start_bot()
        elif path == "/api/bot/stop":
            self._handle_stop_bot()
        elif path == "/api/settings":
            self._handle_save_settings()
        else:
            self._send_text("Not Found", status=404)

    # ------------------------------------------------------------------
    # Status & overview
    # ------------------------------------------------------------------
    def _run_state(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        pid = BotProcessManager.get_running_pid()
        desired = control.read_desired_state()
        telemetry = summary.get("telemetry", {})
        status = _telemetry_value(telemetry, "bot_status")
        if pid is not None:
            state = "paused" if status == "PAUSED_OFFLINE" else "running"
        elif desired["run"]:
            state = "starting"
        else:
            state = "stopped"
        return {"state": state, "pid": pid, "desired_run": desired["run"], "dry_run": desired["dry_run"],
                "desired_updated_at": desired["updated_at"]}

    def _handle_get_status(self):
        summary = self.state_store.get_status_summary()
        run = self._run_state(summary)
        pid = run["pid"]
        uptime_seconds = None
        if pid and os.path.exists(PID_FILE):
            try:
                import time
                uptime_seconds = int(time.time() - os.path.getmtime(PID_FILE))
            except Exception:
                pass
        self._send_json({
            "is_running": pid is not None,
            "status": "RUNNING" if pid else "STOPPED",
            "state": run["state"],
            "pid": pid,
            "uptime_seconds": uptime_seconds,
            "total_candidates": summary.get("total_candidates_found", 0),
            "total_monitored": summary.get("total_monitored_collections", 0),
            "checkpoints": summary.get("checkpoints", {}),
            "telemetry": summary.get("telemetry", {}),
            "bot_timezone": self.config.general.bot_timezone,
        })

    def _handle_get_overview(self):
        import time
        store, cfg = self.state_store, self.config
        summary = store.get_status_summary()
        telemetry = summary.get("telemetry", {})
        run = self._run_state(summary)
        since7 = self._days_ago(6)

        oldest = store.get_oldest_floor_snapshot_ts()
        history_days = max(0.0, (time.time() - oldest) / 86400.0) if oldest else 0.0
        ready_on = None
        if history_days < 7:
            base = oldest or time.time()
            ready_on = time.strftime("%Y-%m-%d", time.localtime(base + 7 * 86400))

        recent = store.get_results_since(self._days_ago(0), limit=25)
        verification = cfg.filters.verification
        self._send_json({
            **run,
            "started_at": _telemetry_value(telemetry, "started_at"),
            "last_check_at": _telemetry_value(telemetry, "last_evaluation_time"),
            "offline_since": _telemetry_value(telemetry, "offline_since"),
            "checked_today": store.count_results_on(self._today()),
            "shortlisted": store.count_shortlisted(),
            "candidates_7d": self._still_passing(store.get_results_since(since7, passes_only=True)),
            "universe": summary.get("total_monitored_collections", 0),
            "skipped_unverified": store.count_skipped_unverified(verification.required_status)
            if cfg.discovery.skip_unverified and verification.enabled else 0,
            "chains_enabled": len(cfg.discovery.chains),
            "chain_counts": store.count_by_chain(),
            "floor_history": {"days": round(history_days, 2), "ready": history_days >= 7, "ready_on": ready_on},
            "funnel": store.get_rejection_funnel(since7),
            "events": store.get_recent_events(20),
            "recent_results": recent,
            "rules_enabled": {k: bool(getattr(getattr(cfg.filters, k), "enabled", True)) for k in RULE_LIMIT_FIELDS},
            "limits": self._limits(),
            "timezone": cfg.general.bot_timezone,
            "autostart_installed": launchd.is_installed(),
        })

    def _limits(self) -> Dict[str, Any]:
        out = {}
        for key, field in RULE_LIMIT_FIELDS.items():
            out[key] = getattr(getattr(self.config.filters, key), field) if field else None
        return out

    # ------------------------------------------------------------------
    # Bot control
    # ------------------------------------------------------------------
    def _handle_start_bot(self):
        payload = self._read_json_body()
        dry_run = bool(payload.get("dry_run", False))
        control.write_desired_state(True, dry_run=dry_run)
        result = BotProcessManager.start_bot(config_path=self.config_path, dry_run=dry_run)
        if not result.get("success") and result.get("pid"):
            result = {"success": True, "pid": result["pid"], "message": "Bot was already running"}
        self._send_json(result, status=200 if result.get("success") else 400)

    def _handle_stop_bot(self):
        control.write_desired_state(False)
        result = BotProcessManager.stop_bot()
        self._send_json(result, status=200 if result.get("success") else 500)

    # ------------------------------------------------------------------
    # Collections, candidates, near misses
    # ------------------------------------------------------------------
    def _handle_get_collections(self, query):
        search = query.get("q", [None])[0]
        limit = int(query.get("limit", [100])[0])
        collections = self.state_store.get_all_monitored_collections(search=search, limit=limit)
        self._send_json({"collections": collections, "count": len(collections)})

    def _still_passing(self, passes) -> int:
        """Collections that passed in the period and haven't failed a later check."""
        newest: Dict[str, str] = {}
        for r in passes:
            if r["created_at"] and r["created_at"] > newest.get(r["slug"], ""):
                newest[r["slug"]] = r["created_at"]
        latest = self.state_store.get_latest_results(newest)
        return sum(1 for slug, found in newest.items()
                   if not (latest.get(slug, {}).get("pass") is False and (latest[slug].get("at") or "") > found))

    def _handle_get_candidates(self, query):
        """Candidates from the bot's records (with details) plus Info.md folders found on disk."""
        days = max(1, min(90, int(query.get("days", [7])[0])))
        items = self.state_store.get_results_since(self._days_ago(days - 1), passes_only=True)

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

        self._send_json({
            "items": items,
            "latest": self.state_store.get_latest_results(i["slug"] for i in items),
            "candidates": candidates,
            "count": len(candidates),
            "history": self.state_store.get_all_candidates_history(limit=50),
        })

    def _safe_info_md(self, path: Optional[str]) -> Optional[str]:
        """Reads an Info.md only if it lives inside the data folder."""
        if not path:
            return None
        root = os.path.realpath(self.config.general.data_root)
        real = os.path.realpath(path)
        if not real.startswith(root + os.sep) or os.path.basename(real) != "Info.md" or not os.path.exists(real):
            return None
        with open(real, "r", encoding="utf-8") as f:
            return f.read()

    def _handle_get_candidate(self, query):
        slug = (query.get("slug", [""])[0] or "").strip()
        date_str = query.get("date", [None])[0]
        if not slug:
            self._send_json({"error": "Missing 'slug'"}, status=400)
            return
        result = self.state_store.get_result(slug, date_str)
        if not result:
            self._send_json({"error": "Not found"}, status=404)
            return
        result["info_md_content"] = self._safe_info_md((result.get("details") or {}).get("info_md"))
        self._send_json(result)

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

        self._send_json({"date": date_str, "project": folder_name, "content": content})

    def _handle_get_near_misses(self):
        """
        Near misses: the latest result per collection in the last 7 days that failed a numeric rule
        (switched on) by at most 50% of its limit, closest first. Early exits stop at the first failed
        rule, so later rules were not checked for those.
        """
        rows = self.state_store.get_results_since(self._days_ago(6))
        seen, near, waiting = set(), [], []
        enabled = {k: bool(getattr(getattr(self.config.filters, k), "enabled", True)) for k in RULE_LIMIT_FIELDS}
        for r in rows:  # newest first
            if r["slug"] in seen:
                continue
            seen.add(r["slug"])
            if r["is_pass"]:
                continue
            d = r.get("details") or {}
            rule = r.get("reject_filter") or d.get("rule")
            if rule and rule.startswith("floor_history"):
                waiting.append(r)
                continue
            value, limit, kind = d.get("value"), d.get("limit"), d.get("kind")
            if rule not in enabled or not enabled[rule] or kind not in ("max", "min"):
                continue
            if not isinstance(value, (int, float)) or not isinstance(limit, (int, float)) or limit <= 0:
                continue
            closeness = abs(value - limit) / limit
            if closeness <= 0.5:
                r["closeness"] = closeness
                near.append(r)
        near.sort(key=lambda r: r["closeness"])
        self._send_json({"near": near[:40], "waiting": waiting[:40]})

    def _handle_inspect_collection(self, query):
        slug = query.get("slug", [None])[0]
        if not slug:
            self._send_json({"error": "Missing 'slug' parameter"}, status=400)
            return

        slug = slug.strip()
        if "opensea.io/collection/" in slug:
            slug = slug.split("opensea.io/collection/")[-1]
        slug = slug.split("?")[0].split("#")[0].strip("/ ").lower()

        # Use the current key from .env, even if it changed after the dashboard started
        client = getattr(getattr(self.evaluator, "provider", None), "client", None)
        if client is not None and hasattr(client, "set_api_key"):
            key = reload_env()
            if key and key != client.api_key:
                client.set_api_key(key)

        try:
            self.evaluator.last_details = None
            report = self.evaluator.evaluate_collection(slug=slug, dry_run=True, stop_on_first_failure=False)
            if not report:
                self._send_json({
                    "slug": slug,
                    "evaluated": False,
                    "error": f"Could not find '{slug}' on OpenSea.",
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
                    "enabled": self.evaluator.filter_engine.is_enabled(name),
                }

            details = self.evaluator.last_details if isinstance(getattr(self.evaluator, "last_details", None), dict) else None
            self._send_json({
                "slug": slug,
                "evaluated": True,
                "is_overall_pass": report.is_overall_pass,
                "rejection_reasons": report.rejection_reasons,
                "criteria": criteria_data,
                "details": details,
            })
        except OpenSeaNetworkError:
            self._send_json({"slug": slug, "evaluated": False, "error": "OpenSea can't be reached. Check the internet connection."}, status=503)
        except Exception as e:
            logger.error("Error inspecting collection %s: %s", slug, e)
            self._send_json({"slug": slug, "evaluated": False, "error": str(e)}, status=500)

    # ------------------------------------------------------------------
    # Settings (saved to config/overrides.yaml; config.yaml keeps its comments)
    # ------------------------------------------------------------------
    def _settings_payload(self) -> Dict[str, Any]:
        cfg = self.config
        rules = {}
        for key, field in RULE_LIMIT_FIELDS.items():
            rc = getattr(cfg.filters, key)
            rules[key] = {"enabled": bool(getattr(rc, "enabled", True)), "value": getattr(rc, field) if field else None}
        tf = cfg.filters.trading_frequency
        rules["trading_frequency"].update({"min_sales_7d": tf.min_sales_7d, "min_floor_sales_7d": tf.min_floor_sales_7d,
                                           "floor_sale_min_pct": tf.floor_sale_min_pct,
                                           "floor_sale_max_pct": tf.floor_sale_max_pct,
                                           "min_offer_sales_14d": tf.min_offer_sales_14d,
                                           "rare_item_pct": tf.rare_item_pct})
        tm = cfg.trade_model
        enabled = set(cfg.discovery.chains)
        chains = []
        for c in CHAINS:
            default_gas = tm.gas_estimate_eth if c.id == "ethereum" else c.gas
            chains.append({
                "id": c.id, "name": c.name, "coin": c.native, "kind": c.kind,
                "enabled": c.id in enabled,
                "gas": tm.chain_gas.get(c.id, default_gas), "default_gas": default_gas,
            })
        return {
            "rules": rules,
            "trade": {"bid": tm.entry_offer_premium_pct, "sell": tm.target_sale_discount_from_floor_pct,
                      "fee": tm.marketplace_fee_pct},
            "chains": chains,
            "skip_unverified": cfg.discovery.skip_unverified,
            "runtime": {
                "wait_for_internet": cfg.runtime.wait_for_internet,
                "notify_new_candidates": cfg.runtime.notify_new_candidates,
                "notification_sound": cfg.runtime.notification_sound,
            },
            "autostart": {"installed": launchd.is_installed()},
        }

    def _handle_get_prices(self):
        book = getattr(type(self), "price_book", None)
        if book is None:
            book = type(self).price_book = PriceBook(self.state_store)
        self._send_json({"prices": book.prices(), "aliases": WRAPPED_TO_NATIVE})

    def _handle_get_settings(self):
        self._send_json(self._settings_payload())

    @staticmethod
    def _number(v: Any, name: str) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number")
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError(f"{name} must be a number")
        if f < 0:
            raise ValueError(f"{name} can't be negative")
        return f

    def _overrides_from_payload(self, p: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        filters: Dict[str, Any] = {}
        for key, rule in (p.get("rules") or {}).items():
            if key not in RULE_LIMIT_FIELDS:
                raise ValueError(f"Unknown rule '{key}'")
            entry: Dict[str, Any] = {}
            if "enabled" in rule:
                entry["enabled"] = bool(rule["enabled"])
            field = RULE_LIMIT_FIELDS[key]
            if field and rule.get("value") is not None:
                entry[field] = self._number(rule["value"], key)
            if key == "trading_frequency":
                for name, label in (("min_sales_7d", "sales in 7 days"), ("min_floor_sales_7d", "sales at floor price"),
                                    ("min_offer_sales_14d", "offer sales in 14 days")):
                    if rule.get(name) is not None:
                        entry[name] = int(self._number(rule[name], label))
                if rule.get("rare_item_pct") is not None:
                    pct = self._number(rule["rare_item_pct"], "rarest items")
                    if pct > 100:
                        raise ValueError("Rarest items must be between 0 and 100%")
                    entry["rare_item_pct"] = pct
            filters[key] = entry
        if filters:
            out["filters"] = filters

        trade = p.get("trade") or {}
        tm: Dict[str, Any] = {}
        for src, dst in (("bid", "entry_offer_premium_pct"), ("sell", "target_sale_discount_from_floor_pct"),
                         ("fee", "marketplace_fee_pct")):
            if trade.get(src) is not None:
                tm[dst] = self._number(trade[src], src)

        chains = p.get("chains")
        if chains is not None:
            ids = [c["id"] for c in chains if c.get("enabled")]
            unknown = [i for i in ids if i not in ALL_CHAIN_IDS]
            if unknown:
                raise ValueError(f"Unknown chain(s): {', '.join(unknown)}")
            out.setdefault("discovery", {})["chains"] = ids
            gas = {c["id"]: self._number(c["gas"], f"gas for {c['id']}") for c in chains
                   if c.get("gas") is not None and c["id"] in ALL_CHAIN_IDS}
            if gas:
                tm["chain_gas"] = gas
                if "ethereum" in gas:
                    tm["gas_estimate_eth"] = gas["ethereum"]
        if tm:
            out["trade_model"] = tm
        if p.get("skip_unverified") is not None:
            out.setdefault("discovery", {})["skip_unverified"] = bool(p["skip_unverified"])

        rt = p.get("runtime") or {}
        rto = {k: bool(rt[k]) for k in ("wait_for_internet", "notify_new_candidates", "notification_sound") if k in rt}
        if rto:
            out["runtime"] = rto
        return out

    def _base_values(self) -> Dict[str, Any]:
        """config.yaml without dashboard overrides, with each chain's effective default gas filled in."""
        base = load_config(self.config_path, use_overrides=False)
        d = base.model_dump()
        gas = dict(base.trade_model.chain_gas)
        for c in CHAINS:
            gas.setdefault(c.id, base.trade_model.gas_estimate_eth if c.id == "ethereum" else c.gas)
        d["trade_model"]["chain_gas"] = gas
        return d

    def _handle_save_settings(self):
        payload = self._read_json_body()
        try:
            new_overrides = self._overrides_from_payload(payload)
        except (ValueError, KeyError, TypeError) as e:
            self._send_json({"success": False, "error": str(e)}, status=400)
            return

        path = overrides_path_for(self.config_path)
        previous = None
        current: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                previous = f.read()
            current = yaml.safe_load(previous) or {}
        # Settings sent now replace what was saved before for the same keys; values equal to
        # config.yaml are left out, so later edits to config.yaml (or new defaults) still apply.
        for keys in _leaf_paths(new_overrides):
            _delete_path(current, keys)
        merged = deep_merge(current, _prune_same(new_overrides, self._base_values()))
        _drop_empty(merged)

        header = "# Saved by the dashboard's Settings page. Values here override config.yaml.\n"
        if merged:
            control.atomic_write(path, header + yaml.safe_dump(merged, sort_keys=True))
        elif os.path.exists(path):
            os.remove(path)
        try:
            new_cfg = load_config(self.config_path)
        except Exception as e:
            if previous is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                control.atomic_write(path, previous)
            self._send_json({"success": False, "error": f"Settings not saved: {e}"}, status=400)
            return

        cls = type(self)
        cls.config = new_cfg
        cls.evaluator = CollectionEvaluator(self.evaluator.provider, self.state_store, new_cfg)
        self.config, self.evaluator = new_cfg, cls.evaluator
        self.state_store.log_event("settings", "Settings saved from the dashboard.")
        self._send_json({"success": True, "settings": self._settings_payload()})

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------
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


def run_dashboard_server(host: str = "127.0.0.1", port: int = 5050, config_path: str = "config/config.yaml",
                         supervise: bool = True):
    """Starts the dashboard HTTP server and the bot supervisor."""
    cfg = load_config(config_path)
    store = StateStore(cfg.general.state_db_path)
    use_ipv4_only(cfg.runtime.ipv4_only)
    client = OpenSeaClient(api_key=cfg.opensea_api_key, request_delay=cfg.scheduler.request_delay_seconds)
    provider = OpenSeaProvider(client)
    evaluator = CollectionEvaluator(provider, store, cfg)

    # Attach to request handler class
    DashboardRequestHandler.config = cfg
    DashboardRequestHandler.state_store = store
    DashboardRequestHandler.evaluator = evaluator
    DashboardRequestHandler.config_path = config_path
    DashboardRequestHandler.price_book = PriceBook(store)

    if supervise:
        sup = control.Supervisor(config_path=config_path, state_store=store)
        threading.Thread(target=sup.run_forever, name="bot-supervisor", daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), DashboardRequestHandler)
    print(f"\n=======================================================")
    print(f"NFT Monitor dashboard running at: http://{host}:{port}")
    print(f"=======================================================\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        httpd.server_close()
