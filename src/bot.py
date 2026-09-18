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
from .config.settings import load_config, BotConfig
from .storage.state_store import StateStore
from .providers.opensea.client import OpenSeaClient
from .providers.opensea.provider import OpenSeaProvider
from .discovery.engine import DiscoveryEngine
from .collectors.orchestrator import CollectionEvaluator
from .utils.logging import setup_logger

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

    def __init__(self, config: Optional[BotConfig] = None):
        self.config = config or load_config()
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
        logger.info("Shutdown signal received (%d). Stopping bot gracefully...", signum)
        self._running = False

    def close(self):
        """Cleanly releases all network and storage resources."""
        self.client.close()
        logger.info("Bot stopped. Resources released.")

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

        last_discovery_time = 0.0
        last_evaluation_time = 0.0

        while self._running:
            now = time.time()

            # 1. Independent Discovery Cycle (Progressive crawl of /api/v2/collections)
            if (now - last_discovery_time >= self.config.scheduler.discovery_interval_seconds) or (last_discovery_time == 0.0):
                logger.info("Initiating progressive catalog discovery cycle...")
                try:
                    new_slugs = self.discovery.discover_next_batch()
                    self.state_store.add_discovered_slugs(new_slugs, source="discovery")
                    last_discovery_time = now
                    self.state_store.update_telemetry("last_discovery_time", now)
                    self.state_store.update_telemetry("total_monitored_universe", self.state_store.get_monitored_collection_count())
                    logger.info("Discovery cycle finished. Monitored universe now contains %d collections.", self.state_store.get_monitored_collection_count())
                except Exception as e:
                    logger.error("Error during progressive discovery cycle: %s", e)

            # 2. Independent Candidate Evaluation / Refresh Cycle
            if (now - last_evaluation_time >= self.config.scheduler.candidate_refresh_interval_seconds) or (last_evaluation_time == 0.0):
                # Retrieve collections due for evaluation (oldest evaluated first)
                slugs_to_eval = self.state_store.get_collections_due_for_evaluation(limit=10)
                if slugs_to_eval:
                    logger.info("Starting candidate refresh cycle for %d collections...", len(slugs_to_eval))
                    for slug in slugs_to_eval:
                        if not self._running:
                            break
                        try:
                            self.evaluator.evaluate_collection(slug=slug, dry_run=dry_run, stop_on_first_failure=True)
                            self.state_store.mark_collection_evaluated(slug)
                        except Exception as e:
                            logger.error("Unexpected error evaluating collection %s: %s", slug, e)

                    last_evaluation_time = now
                    self.state_store.update_telemetry("last_evaluation_time", now)
                else:
                    logger.info("Candidate refresh cycle: no collections currently in monitored universe.")
                    last_evaluation_time = now

            # Sleep briefly before next timer tick
            time.sleep(1.0)

        self.state_store.update_telemetry("bot_status", "STOPPED")
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

    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.command in ("ui", "dashboard"):
        from src.dashboard.server import run_dashboard_server
        run_dashboard_server(host=args.host, port=args.port, config_path=args.config)
        return

    bot = NFTBot(cfg)

    if args.command == "inspect":
        is_dry = not args.no_dry_run
        bot.inspect_collection(args.collection, dry_run=is_dry)
    elif args.command == "status":
        bot.show_status()
    else:
        bot.run_daemon(dry_run=args.dry_run)

if __name__ == "__main__":
    main()
