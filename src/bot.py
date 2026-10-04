import os
import sys
import time
import signal
import argparse
import warnings

# Suppress LibreSSL warning on macOS with system Python
try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

from typing import Optional, List
from .config.settings import load_config, BotConfig, overrides_path_for
from .providers.opensea.client import OpenSeaNetworkError
from .runtime import control
from .storage.state_store import StateStore
from .providers.opensea.client import OpenSeaClient
from .providers.opensea.provider import OpenSeaProvider
from .discovery.engine import DiscoveryEngine
from .collectors.orchestrator import CollectionEvaluator
from .utils.logging import setup_logger
from .utils.time import now_local
from datetime import timedelta

logger = setup_logger("bot_main")

def extract_slug_from_input(slug_or_url: str) -> str:
    """Extracts collection slug from either raw slug or OpenSea collection URL."""
    s = slug_or_url.strip()
    if "opensea.io/collection/" in s:
        # e.g. https://opensea.io/collection/doodles-official/ -> doodles-official
        parts = s.split("opensea.io/collection/")[-1].split("/")
        return parts[0].strip()
    return s.rstrip("/")

class NFTBot:
    """Production-ready 24/7 NFT Collection Monitoring & Filtering Bot."""

    def __init__(self, config: Optional[BotConfig] = None, config_path: Optional[str] = None):
        self.config = config or load_config(config_path or "config/config.yaml")
        self.config_path = config_path
        self._config_mtimes = self._read_config_mtimes()
        self.state_store = StateStore(self.config.general.state_db_path)
        self.client = OpenSeaClient(
            api_key=self.config.opensea_api_key,
            max_retries=self.config.scheduler.max_retries,
            backoff_factor=self.config.scheduler.rate_limit_backoff_factor,
            request_delay=self.config.scheduler.request_delay_seconds,
        )
        self.provider = OpenSeaProvider(self.client)
        self.discovery = DiscoveryEngine(
            provider=self.provider,
            state_store=self.state_store,
            config=self.config.discovery,
        )
        self.evaluator = CollectionEvaluator(
            provider=self.provider,
            state_store=self.state_store,
            config=self.config,
        )
        # Seed watchlist into monitored collections universe if present
        if self.config.discovery.watchlist:
            self.state_store.add_discovered_slugs(self.config.discovery.watchlist, source="watchlist")

        self._running = False
        self._setup_signals()

    def _setup_signals(self):
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        if not self._running and signum == signal.SIGTERM:
            raise SystemExit(0)  # second request: stop now
        logger.info("Shutdown signal received (%d). Stopping bot gracefully...", signum)
        self._running = False
        self.client.stop_event.set()  # cut short any retry wait in progress

    def close(self):
        """Cleanly releases all network and storage resources."""
        self.client.close()
        logger.info("Bot stopped. Resources released.")

    # ------------------------------------------------------------------
    # Live settings: the dashboard saves config/overrides.yaml; pick it up without a restart
    # ------------------------------------------------------------------
    def _read_config_mtimes(self):
        if not self.config_path:
            return None
        out = []
        for p in (self.config_path, overrides_path_for(self.config_path)):
            try:
                out.append(os.path.getmtime(p))
            except OSError:
                out.append(None)
        return tuple(out)

    def _maybe_reload_config(self) -> None:
        mt = self._read_config_mtimes()
        if mt is None or mt == self._config_mtimes:
            return
        self._config_mtimes = mt
        try:
            new_cfg = load_config(self.config_path)
        except Exception as e:
            logger.error("Settings changed but could not be loaded (%s). Keeping the previous settings.", e)
            return
        self.config = new_cfg
        self.discovery.config = new_cfg.discovery
        self.evaluator = CollectionEvaluator(provider=self.provider, state_store=self.state_store, config=new_cfg)
        logger.info("Settings changed. Using the new settings from the next check.")
        self.state_store.log_event("settings", "New settings are in use.")

    def _due_collections(self):
        verification = self.config.filters.verification
        allowed = None
        if self.config.discovery.skip_unverified and verification.enabled:
            allowed = verification.required_status
        return self.state_store.get_collections_due_for_evaluation(
            limit=self.config.scheduler.evaluations_per_cycle,
            shortlist_refresh_seconds=self.config.scheduler.shortlist_refresh_seconds,
            chains=self.config.discovery.chains or None,
            allowed_statuses=allowed,
        )

    # ------------------------------------------------------------------
    # Offline handling: pause instead of marking collections as failed
    # ------------------------------------------------------------------
    def _wait_until_online(self) -> None:
        rt = self.config.runtime
        if control.is_online(rt.connectivity_host):
            return
        started = time.time()
        logger.warning("No internet connection. Pausing until it is back...")
        self.state_store.update_telemetry("bot_status", "PAUSED_OFFLINE")
        self.state_store.update_telemetry("offline_since", started)
        self.state_store.log_event("offline", "No internet. The bot paused and will carry on by itself.")
        while self._running and not control.is_online(rt.connectivity_host):
            for _ in range(max(1, rt.offline_check_seconds)):
                if not self._running:
                    break
                time.sleep(1.0)
        if self._running:
            minutes = (time.time() - started) / 60.0
            self.state_store.update_telemetry("bot_status", "RUNNING")
            self.state_store.log_event("online", f"Internet is back after {minutes:.0f} minutes. Carrying on.")
            logger.info("Internet connection restored after %.1f minutes.", minutes)

    def run_daemon(self, dry_run: bool = False):
        """
        Runs the 24/7 monitoring loop with independently scheduled:
        1. Progressive catalog discovery crawl (cadence: discovery_interval_seconds)
        2. Market-data evaluation & candidate refresh (cadence: candidate_refresh_interval_seconds)
        """
        self._running = True
        logger.info(
            "Starting 24/7 NFT Monitoring Bot daemon (dry_run=%s, discovery_cadence=%ds, eval_cadence=%ds)...",
            dry_run,
            self.config.scheduler.discovery_interval_seconds,
            self.config.scheduler.candidate_refresh_interval_seconds,
        )
        self.state_store.update_telemetry("bot_status", "RUNNING")
        self.state_store.update_telemetry("started_at", time.time())
        control.write_pid_file(os.getpid())
        self.state_store.log_event("start", "Bot started" + (" (dry run: no Info.md files)." if dry_run else "."))

        last_discovery_time = 0.0
        last_evaluation_time = 0.0

        while self._running:
            self._maybe_reload_config()
            if self.config.runtime.wait_for_internet:
                self._wait_until_online()
                if not self._running:
                    break
            now = time.time()

            # 1. Independent Discovery Cycle (Progressive crawl of /api/v2/collections)
            if (now - last_discovery_time >= self.config.scheduler.discovery_interval_seconds) or (last_discovery_time == 0.0):
                logger.info("Initiating progressive catalog discovery cycle...")
                try:
                    new_slugs = self.discovery.discover_next_batch()
                    meta = getattr(self.provider, "last_discovery_meta", {}) or {}
                    self.state_store.add_discovered_slugs(new_slugs, source="discovery", meta=meta)
                    meta.clear()
                    last_discovery_time = now
                    self.state_store.update_telemetry("last_discovery_time", now)
                    self.state_store.update_telemetry("total_monitored_universe", self.state_store.get_monitored_collection_count())
                    logger.info("Discovery cycle finished. Monitored universe now contains %d collections.", self.state_store.get_monitored_collection_count())
                except OpenSeaNetworkError as e:
                    logger.warning("Discovery skipped: OpenSea unreachable (%s).", e)
                    continue
                except Exception as e:
                    logger.error("Error during progressive discovery cycle: %s", e)

            # 2. Independent Candidate Evaluation / Refresh Cycle
            if (now - last_evaluation_time >= self.config.scheduler.candidate_refresh_interval_seconds) or (last_evaluation_time == 0.0):
                # Retrieve collections due for evaluation (oldest evaluated first)
                slugs_to_eval = self._due_collections()
                if slugs_to_eval:
                    logger.info("Starting candidate refresh cycle for %d collections...", len(slugs_to_eval))
                    went_offline = False
                    for slug in slugs_to_eval:
                        if not self._running:
                            break
                        try:
                            self.evaluator.evaluate_collection(slug=slug, dry_run=dry_run, stop_on_first_failure=True)
                            self.state_store.mark_collection_evaluated(slug)
                        except OpenSeaNetworkError as e:
                            # Not the collection's fault: leave it unmarked and retry when back online
                            logger.warning("OpenSea unreachable while checking %s (%s).", slug, e)
                            went_offline = True
                            break
                        except Exception as e:
                            logger.error("Unexpected error evaluating collection %s: %s", slug, e)
                    if went_offline:
                        continue

                    last_evaluation_time = now
                    self.state_store.update_telemetry("last_evaluation_time", now)
                else:
                    logger.info("Candidate refresh cycle: no collections currently in monitored universe.")
                    last_evaluation_time = now

            # Sleep briefly before next timer tick
            time.sleep(1.0)

        self.state_store.update_telemetry("bot_status", "STOPPED")
        self.state_store.log_event("stop", "Bot stopped.")
        control.remove_pid_file(only_if_pid=os.getpid())
        self.close()

    def inspect_collection(self, slug_or_url: str, dry_run: bool = True):
        """Diagnostic inspection: evaluates all metrics for a collection without short-circuiting."""
        slug = extract_slug_from_input(slug_or_url)
        print(f"\n=================================================================")
        print(f"DIAGNOSTIC INSPECTION: {slug}")
        print(f"=================================================================\n")

        report = self.evaluator.evaluate_collection(
            slug=slug,
            dry_run=dry_run,
            stop_on_first_failure=False,  # Run all filters so user sees complete diagnostic
        )

        if not report:
            print(f"FAILED TO EVALUATE: Could not retrieve data for collection '{slug}'.")
            return

        print(f"Overall Result: {'PASS' if report.is_overall_pass else 'FAIL'}\n")
        print("FILTER BREAKDOWN:")
        for name, crit in report.criteria.items():
            status_symbol = "✓" if crit.result.value in ("PASS", "OBSERVE") else "✗"
            print(f"  [{status_symbol}] {name.upper()}:")
            print(f"      Threshold:    {crit.threshold}")
            print(f"      Actual Value: {crit.actual_value}{crit.unit}")
            print(f"      Result:       {crit.result.value} (Data Quality: {crit.data_quality.value})")
            print(f"      Formula:      {crit.formula}")
            if crit.notes:
                print(f"      Notes:        {crit.notes}")

        if report.rejection_reasons:
            print("\nREJECTION REASONS:")
            for reason in report.rejection_reasons:
                print(f"  - {reason}")

        print("\n=================================================================\n")

    def show_status(self):
        """Displays health, checkpoint, and candidate counts."""
        summary = self.state_store.get_status_summary()
        print("\n================ BOT STATUS SUMMARY ================")
        print(f"Total Candidates Recorded: {summary.get('total_candidates_found', 0)}")
        print(f"Total Monitored Universe:  {summary.get('total_monitored_collections', 0)}")
        print("\nDiscovery Checkpoints:")
        for source, info in summary.get("checkpoints", {}).items():
            print(f"  - Source '{source}': cursor={info.get('cursor') or 'START'}, updated_at={info.get('updated_at')}")
        since = (now_local(self.config.general.bot_timezone).date() - timedelta(days=6)).strftime("%Y-%m-%d")
        funnel = self.state_store.get_rejection_funnel(since)
        print(f"\nRejection Funnel (last 7 days, since {since}; latest result per collection per day):")
        if funnel:
            for name, count in funnel.items():
                print(f"  - {name}: {count}")
        else:
            print("  - no evaluations recorded yet")
        print("\nTelemetry:")
        for k, v in summary.get("telemetry", {}).items():
            print(f"  - {k}: {v.get('value')} (at {v.get('updated_at')})")
        print("====================================================\n")

def main():
    parser = argparse.ArgumentParser(description="24/7 NFT Collection Monitoring & Filtering Bot")
    parser.add_argument("--dry-run", action="store_true", help="Run without creating candidate files on filesystem")
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Path to configuration file")

    subparsers = parser.add_subparsers(dest="command")
    
    # inspect command
    inspect_parser = subparsers.add_parser("inspect", help="Deeply inspect a single collection by slug or URL")
    inspect_parser.add_argument("collection", type=str, help="Collection slug or OpenSea URL")
    inspect_parser.add_argument("--no-dry-run", action="store_true", help="Write Info.md if collection passes")

    # status command
    subparsers.add_parser("status", help="Show bot health and discovery status")

    # ui / dashboard command
    dashboard_parser = subparsers.add_parser("ui", aliases=["dashboard"], help="Launch local web UI dashboard")
    dashboard_parser.add_argument("--port", type=int, default=5050, help="Port to listen on (default: 5050)")
    dashboard_parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")

    # macOS auto-start (launchd)
    install_parser = subparsers.add_parser("install-autostart", help="macOS: open the dashboard at login and keep the bot running")
    install_parser.add_argument("--port", type=int, default=5050)
    subparsers.add_parser("uninstall-autostart", help="macOS: turn auto-start off")

    args = parser.parse_args()

    if args.command == "install-autostart":
        from .runtime import launchd
        print(launchd.install(args.config, port=args.port))
        return
    if args.command == "uninstall-autostart":
        from .runtime import launchd
        print(launchd.uninstall())
        return

    cfg = load_config(args.config)

    if args.command in ("ui", "dashboard"):
        from src.dashboard.server import run_dashboard_server
        run_dashboard_server(host=args.host, port=args.port, config_path=args.config)
        return

    bot = NFTBot(cfg, config_path=args.config)

    if args.command == "inspect":
        is_dry = not args.no_dry_run
        bot.inspect_collection(args.collection, dry_run=is_dry)
    elif args.command == "status":
        bot.show_status()
    else:
        bot.run_daemon(dry_run=args.dry_run)

if __name__ == "__main__":
    main()
