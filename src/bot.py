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
        """Runs the 24/7 monitoring loop with independent discovery and evaluation schedules."""
        self._running = True
        logger.info("Starting 24/7 NFT Monitoring Bot daemon (dry_run=%s)...", dry_run)
        self.state_store.update_telemetry("bot_status", "RUNNING")

        last_discovery_time = 0.0
        pending_queue: List[str] = []

        while self._running:
            now = time.time()

            # 1. Independent Discovery Cycle
            if now - last_discovery_time >= self.config.scheduler.discovery_interval_seconds or last_discovery_time == 0.0:
                logger.info("Initiating progressive discovery cycle...")
                try:
                    new_slugs = self.discovery.discover_next_batch()
                    for s in new_slugs:
                        if s not in pending_queue:
                            pending_queue.append(s)
                    last_discovery_time = now
                    self.state_store.update_telemetry("last_discovery_time", now)
                    self.state_store.update_telemetry("pending_queue_size", len(pending_queue))
                except Exception as e:
                    logger.error("Error during discovery cycle: %s", e)

            # 2. Candidate Evaluation Cycle
            if pending_queue:
                # Process a batch of pending collections
                batch_to_eval = pending_queue[:10]
                pending_queue = pending_queue[10:]
                logger.info("Evaluating batch of %d collections (%d remaining in queue)...", len(batch_to_eval), len(pending_queue))

                for slug in batch_to_eval:
                    if not self._running:
                        break
                    try:
                        self.evaluator.evaluate_collection(slug=slug, dry_run=dry_run, stop_on_first_failure=True)
                    except Exception as e:
                        logger.error("Unexpected error evaluating collection %s: %s", slug, e)

            # Sleep briefly before next check
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

    args = parser.parse_args()

    cfg = load_config(args.config)
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
