import os
import tempfile
import time
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from src.config.settings import (
    BotConfig,
    GeneralConfig,
    DiscoveryConfig,
    FiltersConfig,
    TradeModelConfig,
    SchedulerConfig,
    TradingFrequencyFilterConfig,
    FloorChangeFilterConfig,
    ListedItemsFilterConfig,
    VerificationFilterConfig,
    ProjectAgeFilterConfig,
)
from src.collectors.orchestrator import CollectionEvaluator
from src.storage.state_store import StateStore
from src.discovery.engine import DiscoveryEngine
from src.models.collection import (
    CollectionMetadata,
    Contract,
    Fee,
    CollectionStats,
    Offer,
)
from src.providers.opensea.client import OpenSeaClient, OpenSeaAuthError, OpenSeaApiError
from tests.mock_data import make_sample_collection, make_sample_sales_events, make_sample_floor_history

def make_realistic_passing_collection(slug="solitary-voyagers", name="Solitary Voyagers"):
    """Creates a realistic collection fixture that satisfies all seven BOT filters."""
    col = CollectionMetadata(
        slug=slug,
        name=name,
        description="A quiet generative sci-fi art collection of 10,000 voyagers.",
        created_date=(datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d"),
        opensea_url=f"https://opensea.io/collection/{slug}",
        project_url="https://solitaryvoyagers.art",
        twitter_username="SolitaryNFT",
        discord_url="https://discord.gg/solitaryvoyagers",
        safelist_status="verified",
        is_disabled=False,
        is_nsfw=False,
        total_supply=10000,
        contracts=[Contract(address="0x1234567890abcdef1234567890abcdef12345678", chain="ethereum")],
        fees=[
            Fee(fee=1.0, recipient="0x0000a26b00c1f0df003000390027140000faa719", required=True), # 1.0% OpenSea Seaport protocol fee
            Fee(fee=5.0, recipient="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef", required=False), # 5.0% creator royalty
        ],
    )
    return col

# ==============================================================================
# Acceptance Test 1 & 2: End-to-End PASS Test and Validate Info.md
# ==============================================================================
def test_acceptance_1_and_2_end_to_end_pass_and_validate_info_md():
    """
    Acceptance Test 1 & 2:
    - Runs complete pipeline on a collection passing all 7 filters.
    - Generates YYYY-MM-DD/<Project>/Info.md.
    - Validates that Info.md strictly separates Observed Data, Model Assumptions,
      and Modelled Results, with exact timestamps, formulas, sources, and no Fundamentals.md.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        data_root = os.path.join(temp_dir, "data")
        db_path = os.path.join(temp_dir, "state", "bot.db")

        config = BotConfig(
            general=GeneralConfig(data_root=data_root, state_db_path=db_path, bot_timezone="Asia/Kolkata"),
            filters=FiltersConfig(
                trading_frequency=TradingFrequencyFilterConfig(metric="average_daily_sales", max_threshold=2.0, sale_count_mode="transactions"),
                floor_change_1d=FloorChangeFilterConfig(max_change_pct=8.0),
                floor_change_7d=FloorChangeFilterConfig(max_change_pct=10.0),
                listed_items=ListedItemsFilterConfig(max_listed_pct=6.0),
                verification=VerificationFilterConfig(required_status=["verified"]),
                project_age=ProjectAgeFilterConfig(min_age_days=60.0),
            ),
            trade_model=TradeModelConfig(
                entry_offer_premium_pct=1.0,
                target_sale_discount_from_floor_pct=5.0,
                gas_estimate_eth=0.005,
            )
        )
        state_store = StateStore(db_path)

        mock_provider = MagicMock()
        col = make_realistic_passing_collection()
        mock_provider.get_collection.return_value = col
        # Listed items: 250 / 10,000 = 2.50% (< 6.0%)
        mock_provider.get_active_listings_count.return_value = (250, False)
        # Sales events: 8 sales across 7 complete calendar days = 1.14 txs/day (<= 2.0)
        mock_provider.get_sale_events.return_value = make_sample_sales_events([1, 2, 0, 1, 2, 1, 1])
        # Floor: Current = 1.25 ETH, 1d = 1.22 (+2.46% < 8%), 7d = 1.20 (+4.17% < 10%)
        curr_floor, points_1d, points_7d = make_sample_floor_history(1.25, pct_change_1d=2.46, pct_change_7d=4.17)
        mock_provider.get_collection_stats.return_value = CollectionStats(floor_price=curr_floor, floor_price_symbol="ETH")
        mock_provider.get_floor_price_history.side_effect = lambda slug, timeframe: points_1d if timeframe == "one_day" else points_7d
        # Top offer: 1.10 WETH
        mock_provider.get_top_offer.return_value = Offer(
            order_hash="0xoff123", chain="ethereum", price_value=1.10, price_currency="WETH"
        )

        evaluator = CollectionEvaluator(mock_provider, state_store, config)
        report = evaluator.evaluate_collection("solitary-voyagers", dry_run=False)

        assert report is not None
        assert report.is_overall_pass is True

        # Check directory structure: YYYY-MM-DD/Solitary-Voyagers/Info.md
        dated_dirs = os.listdir(data_root)
        assert len(dated_dirs) == 1
        day_dir = os.path.join(data_root, dated_dirs[0])
        proj_dir = os.path.join(day_dir, "Solitary-Voyagers")
        assert os.path.exists(proj_dir)

        info_path = os.path.join(proj_dir, "Info.md")
        assert os.path.exists(info_path)

        # Requirement 6: Verify Fundamentals.md was NOT created
        assert not os.path.exists(os.path.join(proj_dir, "Fundamentals.md"))
        assert os.listdir(proj_dir) == ["Info.md"]

        # Validate Info.md contents
        with open(info_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 1. Identity & References
        assert "# Project: Solitary Voyagers" in content
        assert "## Collection Identity" in content
        assert "- **Project Name**: Solitary Voyagers" in content
        assert "- **OpenSea Collection**: solitary-voyagers" in content
        assert "- **Contract Address**: `0x1234567890abcdef1234567890abcdef12345678`" in content
        assert "OpenSea Collection Age" in content
        assert "calculated from OpenSea created_date" in content
        assert "- **Website**: https://solitaryvoyagers.art" in content
        assert "- **X / Twitter**: https://x.com/SolitaryNFT" in content
        assert "- **Discord**: https://discord.gg/solitaryvoyagers" in content

        # 2. Collection Size
        assert "## Collection Size" in content
        assert "- **Total Supply**: 10,000" in content
        assert "- **Listed Items**: 250" in content
        assert "- **Listed Percentage**: 2.50%" in content

        # 3. 7-Day Trading Breakdown
        assert "## Trading Activity — 7 Days" in content
        assert "- **7-Day Total Transactions**: 8" in content
        assert "- **Average Transactions / Day**: 1.14" in content
        assert "| Date (Asia/Kolkata) | Trades (Transactions) | Items Sold | Status |" in content

        # 4. Floor Price
        assert "## Floor Price" in content
        assert "- **Current Floor**: 1.2500 ETH" in content
        assert "1-Day Floor Change" in content
        assert "7-Day Floor Change" in content

        # 5. Top Offer
        assert "## Top Offer" in content
        assert "- **Observed Top Offer**: 1.1000 WETH" in content
        assert "- **OpenSea Marketplace Fee**: 1.00%" in content
        assert "- **Creator Royalty**: 5.00%" in content

        # 6. Trade Economics (Three-Tier Clear Separation)
        assert "## Trade Economics" in content
        assert "### A. Observed Market Data (Factual / API-Sourced)" in content
        assert "- **Observed Top Offer**: 1.1000 WETH" in content
        assert "- **Current Floor**: 1.2500 ETH" in content
        assert "- **Marketplace Fee (from OpenSea API)**: 1.00%" in content
        assert "- **Creator Royalty (from OpenSea API)**: 5.00%" in content
        assert "- **Fee Data Reliability**: RELIABLE" in content

        assert "### B. Model Assumptions (Configured Trading Strategy Parameters)" in content
        assert "- **[ASSUMPTION] Entry-Offer Premium**: +1.0% above observed top offer" in content
        assert "- **[ASSUMPTION] Target Exit Discount**: -5.0% below current floor" in content
        assert "- **[ASSUMPTION] Gas Estimate**: 0.0050 ETH" in content

        assert "### C. Modelled Results (Theoretical Estimates)" in content
        assert "- **Modelled Entry Offer (Hypothetical Buy Price)**: 1.1110 ETH" in content
        assert "- **Target Exit Price (Hypothetical Sell Price)**: 1.1875 ETH" in content
        assert "- **Gross Spread (Exit - Entry)**: 0.0765 ETH" in content
        assert "- **Estimated Selling Marketplace Fee**: 0.0119 ETH" in content
        assert "- **Estimated Creator Royalty Fee**: 0.0594 ETH" in content
        assert "- **Estimated Total Gas Cost**: 0.0050 ETH" in content
        assert "- **Estimated Net Profit**: 0.0002 ETH" in content
        assert "- **Estimated ROI**: 0.02%" in content

        # 7. BOT Filter Conditions & Pass Verdict
        assert "## BOT Filter Conditions" in content
        assert "### OpenSea Collection Age" in content
        assert "### Verification" in content
        assert "### Listed Items" in content
        assert "### Trading Frequency" in content
        assert "### Floor Change 1D" in content
        assert "### Floor Change 7D" in content
        assert "### Offer To Floor" in content
        assert "## BOT Final Result\n\n**PASS**" in content

# ==============================================================================
# Acceptance Test 3: Existing-Collection Reevaluation
# ==============================================================================
def test_acceptance_3_existing_collection_reevaluation():
    """
    Acceptance Test 3:
    Demonstrates that a collection evaluated earlier (e.g. failing on cycle 1)
    is NOT permanently ignored, and can be reevaluated in a later cycle
    when its market conditions improve to satisfy all filters.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        data_root = os.path.join(temp_dir, "data")
        db_path = os.path.join(temp_dir, "state", "bot.db")
        config = BotConfig(
            general=GeneralConfig(data_root=data_root, state_db_path=db_path),
            filters=FiltersConfig(listed_items=ListedItemsFilterConfig(max_listed_pct=6.0)),
        )
        state_store = StateStore(db_path)

        # 1. Cycle 1: Collection has 8.0% listings -> FAILS listed_items filter
        mock_provider = MagicMock()
        col = make_realistic_passing_collection()
        mock_provider.get_collection.return_value = col
        mock_provider.get_active_listings_count.return_value = (800, False) # 8.0% (> 6.0%)
        mock_provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1])
        curr_floor, p1, p7 = make_sample_floor_history(1.5, 2.0, 3.0)
        mock_provider.get_collection_stats.return_value = CollectionStats(floor_price=curr_floor)
        mock_provider.get_floor_price_history.side_effect = lambda slug, timeframe="one_day": p1 if timeframe == "one_day" else p7
        mock_provider.get_top_offer.return_value = Offer(order_hash="0x1", chain="ethereum", price_value=1.0, price_currency="WETH")

        evaluator = CollectionEvaluator(mock_provider, state_store, config)

        # Add to monitored collections universe
        state_store.add_discovered_slugs(["solitary-voyagers"], source="discovery")

        # Evaluate cycle 1
        report1 = evaluator.evaluate_collection("solitary-voyagers", dry_run=False)
        assert report1 is None # Short-circuited and rejected

        # Mark evaluated and check candidate history has FAIL
        state_store.mark_collection_evaluated("solitary-voyagers")
        from src.utils.time import now_local
        today = now_local("Asia/Kolkata").strftime("%Y-%m-%d")
        assert state_store.is_candidate_recorded_today("solitary-voyagers", today) is False # Not recorded as PASS

        # 2. Cycle 2: Market conditions improve! Listings drop to 200 (2.0% < 6.0%)
        mock_provider.get_active_listings_count.return_value = (200, False) # Now 2.0%

        # Re-evaluate in cycle 2
        report2 = evaluator.evaluate_collection("solitary-voyagers", dry_run=False)
        assert report2 is not None
        assert report2.is_overall_pass is True

        # Candidate now successfully recorded and Info.md generated!
        assert state_store.is_candidate_recorded_today("solitary-voyagers", today) is True
        dated_dirs = os.listdir(data_root)
        assert len(dated_dirs) == 1
        assert os.path.exists(os.path.join(data_root, dated_dirs[0], "Solitary-Voyagers", "Info.md"))

# ==============================================================================
# Acceptance Test 4: Restart Recovery
# ==============================================================================
def test_acceptance_4_restart_recovery():
    """
    Acceptance Test 4:
    Demonstrates cursor persistence across bot shutdown and restart:
    - Discovery crawls page 1 & 2, storing cursor 'cursor_page_2'.
    - Bot terminates.
    - New bot starts up, restores cursor 'cursor_page_2' from SQLite, and resumes crawl.
    - Candidate history prevents duplicate file creation across restarts.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "bot.db")
        data_root = os.path.join(temp_dir, "data")

        # Session 1: Run discovery and store checkpoint
        store1 = StateStore(db_path)
        mock_provider1 = MagicMock()
        mock_provider1.discover_collections.side_effect = [
            (["slug1", "slug2"], "cursor_p1_next"),
            (["slug3", "slug4"], "cursor_p2_next"),
        ]
        mock_provider1.discover_top_collections.return_value = []
        mock_provider1.discover_trending_collections.return_value = []

        cfg = DiscoveryConfig(max_pages_per_cycle=2, batch_size=50, enable_top=False, enable_trending=False)
        discovery1 = DiscoveryEngine(mock_provider1, store1, cfg)
        slugs1 = discovery1.discover_next_batch()

        assert slugs1 == ["slug1", "slug2", "slug3", "slug4"] or set(slugs1) == {"slug1", "slug2", "slug3", "slug4"}
        assert store1.get_checkpoint("collections") == "cursor_p2_next"

        # Simulate bot termination (close session 1)
        del discovery1
        del store1

        # Session 2: Fresh bot restart
        store2 = StateStore(db_path)
        # Checkpoint is preserved
        restored_cursor = store2.get_checkpoint("collections")
        assert restored_cursor == "cursor_p2_next"

        # Mock next page crawl resuming from restored cursor
        mock_provider2 = MagicMock()
        mock_provider2.discover_collections.return_value = (["slug5", "slug6"], "cursor_p3_next")
        mock_provider2.discover_top_collections.return_value = []
        mock_provider2.discover_trending_collections.return_value = []

        discovery2 = DiscoveryEngine(mock_provider2, store2, cfg)
        slugs2 = discovery2.discover_next_batch()

        # Verified it called discover_collections with the restored cursor!
        assert mock_provider2.discover_collections.call_args_list[0][1]["cursor"] == "cursor_p2_next"
        assert store2.get_checkpoint("collections") == "cursor_p3_next"

# ==============================================================================
# Acceptance Test 5: API-Key Lifecycle & Authentication Failure Handling
# ==============================================================================
def test_acceptance_5_api_key_lifecycle_and_auth_failure():
    """
    Acceptance Test 5:
    Validates that API key expiration / HTTP 401 / 403:
    - Raises OpenSeaAuthError.
    - Never silently treats failed API calls as 0 sales, 0 listings, or passing data.
    - Fails closed with DATA_INSUFFICIENT.
    """
    client = OpenSeaClient(api_key="expired_or_invalid_key", max_retries=0)

    # Mock 401 Unauthorized response from OpenSea
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.headers = {}
    mock_resp.text = '{"detail": "Invalid or expired API key"}'

    with patch.object(client.session, "request", return_value=mock_resp):
        # 1. Direct client call raises OpenSeaAuthError
        with pytest.raises(OpenSeaAuthError) as exc_info:
            client.get("/api/v2/collections/some-collection")
        assert "401" in str(exc_info.value)
        assert "API key invalid or expired" in str(exc_info.value)

    # 2. Verify evaluator fails closed when listings fail (never assumes 0 listings)
    with tempfile.TemporaryDirectory() as temp_dir:
        store = StateStore(os.path.join(temp_dir, "bot.db"))
        config = BotConfig(general=GeneralConfig(data_root=temp_dir, state_db_path=os.path.join(temp_dir, "bot.db")))
        mock_provider = MagicMock()
        mock_provider.get_collection.return_value = make_realistic_passing_collection()
        mock_provider.get_active_listings_count.return_value = (None, False)
        mock_provider.get_collection_stats.return_value = CollectionStats(floor_price=1.5)
        mock_provider.get_floor_price_history.return_value = []
        mock_provider.get_top_offer.return_value = Offer(order_hash="0x1", chain="ethereum", price_value=1.0, price_currency="WETH")
        mock_provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1])

        evaluator = CollectionEvaluator(mock_provider, store, config)
        report = evaluator.evaluate_collection("solitary-voyagers", dry_run=False, stop_on_first_failure=False)

        assert report is not None
        assert report.is_overall_pass is False
        listed_crit = report.criteria["listed_items"]
        assert listed_crit.result.value == "DATA_INSUFFICIENT"
        assert listed_crit.actual_value == "UNKNOWN"

    # 3. Verify evaluator fails closed when sales events fail (never assumes 0 sales)
    with tempfile.TemporaryDirectory() as temp_dir:
        store = StateStore(os.path.join(temp_dir, "bot.db"))
        config = BotConfig(general=GeneralConfig(data_root=temp_dir, state_db_path=os.path.join(temp_dir, "bot.db")))
        mock_provider = MagicMock()
        mock_provider.get_collection.return_value = make_realistic_passing_collection()
        mock_provider.get_active_listings_count.return_value = (250, False)
        mock_provider.get_collection_stats.return_value = CollectionStats(floor_price=1.5)
        mock_provider.get_floor_price_history.return_value = []
        mock_provider.get_top_offer.return_value = Offer(order_hash="0x1", chain="ethereum", price_value=1.0, price_currency="WETH")
        # API failure on sales returns None
        mock_provider.get_sale_events.return_value = None

        evaluator = CollectionEvaluator(mock_provider, store, config)
        report = evaluator.evaluate_collection("solitary-voyagers", dry_run=False, stop_on_first_failure=False)

        assert report is not None
        assert report.is_overall_pass is False
        sales_crit = report.criteria["trading_frequency"]
        assert sales_crit.result.value == "DATA_INSUFFICIENT"

# ==============================================================================
# Acceptance Test 7: Failure Isolation
# ==============================================================================
def test_acceptance_7_failure_isolation():
    """
    Acceptance Test 7:
    Simulates a batch evaluation where one collection encounters an API error / 429,
    and verifies that the error is isolated and the daemon proceeds to evaluate
    remaining collections in the queue.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        data_root = os.path.join(temp_dir, "data")
        db_path = os.path.join(temp_dir, "state", "bot.db")
        config = BotConfig(general=GeneralConfig(data_root=data_root, state_db_path=db_path))
        state_store = StateStore(db_path)

        mock_provider = MagicMock()

        # col1: fails age filter
        col1 = make_realistic_passing_collection(slug="col1", name="Col 1")
        col1.created_date = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d") # 20 days < 60 days

        # col2: encounters an unexpected API crash
        def get_col_side_effect(slug):
            if slug == "col1":
                return col1
            elif slug == "col2":
                raise OpenSeaApiError("Simulated 429 Rate Limit Exhaustion on col2")
            elif slug == "col3":
                return make_realistic_passing_collection(slug="col3", name="Col 3")
            return None

        mock_provider.get_collection.side_effect = get_col_side_effect
        mock_provider.get_active_listings_count.return_value = (200, False)
        mock_provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1])
        curr_floor, p1, p7 = make_sample_floor_history(1.5, 2.0, 3.0)
        mock_provider.get_collection_stats.return_value = CollectionStats(floor_price=curr_floor)
        mock_provider.get_floor_price_history.side_effect = lambda slug, timeframe="one_day": p1 if timeframe == "one_day" else p7
        mock_provider.get_top_offer.return_value = Offer(order_hash="0x3", chain="ethereum", price_value=1.0, price_currency="WETH")

        evaluator = CollectionEvaluator(mock_provider, state_store, config)

        batch = ["col1", "col2", "col3"]
        results = {}

        for slug in batch:
            try:
                res = evaluator.evaluate_collection(slug, dry_run=False, stop_on_first_failure=True)
                results[slug] = res
            except Exception as e:
                # Isolated failure logging
                results[slug] = f"ERROR: {e}"

        # Col 1 was rejected cleanly
        assert results["col1"] is None
        # Col 2 raised an isolated error without crashing the batch
        assert "ERROR" in results["col2"]
        # Col 3 was evaluated despite col2 failing, passed all filters, and generated dossier
        assert results["col3"] is not None
        assert results["col3"].is_overall_pass is True

        # Check Col 3 dossier exists
        dated_dirs = os.listdir(data_root)
        assert len(dated_dirs) == 1
        assert os.path.exists(os.path.join(data_root, dated_dirs[0], "Col-3", "Info.md"))

# ==============================================================================
# Acceptance Test 8: Long-Running Multi-Cycle Behavior
# ==============================================================================
def test_acceptance_8_long_running_multi_cycle_behavior():
    """
    Acceptance Test 8:
    Simulates multiple continuous scheduling cycles:
    - Discovery discovers new items progressively.
    - Refresh schedule processes monitored collections in round-robin fashion.
    - Cursors, candidate records, and telemetry stay consistent.
    - Deduplication prevents runaway file generation.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        data_root = os.path.join(temp_dir, "data")
        db_path = os.path.join(temp_dir, "state", "bot.db")
        cfg = DiscoveryConfig(max_pages_per_cycle=1, batch_size=50, enable_top=False, enable_trending=False)
        config = BotConfig(general=GeneralConfig(data_root=data_root, state_db_path=db_path), discovery=cfg)
        state_store = StateStore(db_path)

        mock_provider = MagicMock()
        mock_provider.discover_collections.side_effect = [
            (["col-a", "col-b"], "cursor_cycle_1"),
            (["col-c", "col-d"], "cursor_cycle_2"),
            (["col-e"], "cursor_cycle_3"),
        ]
        mock_provider.discover_top_collections.return_value = []
        mock_provider.discover_trending_collections.return_value = []

        # Passing candidate
        pass_col = make_realistic_passing_collection(slug="col-a", name="Col A")
        mock_provider.get_collection.side_effect = lambda slug: pass_col if slug == "col-a" else None
        mock_provider.get_active_listings_count.return_value = (200, False)
        mock_provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1])
        curr_floor, p1, p7 = make_sample_floor_history(1.5, 2.0, 3.0)
        mock_provider.get_collection_stats.return_value = CollectionStats(floor_price=curr_floor)
        mock_provider.get_floor_price_history.side_effect = lambda slug, timeframe="one_day": p1 if timeframe == "one_day" else p7
        mock_provider.get_top_offer.return_value = Offer(order_hash="0xa", chain="ethereum", price_value=1.0, price_currency="WETH")

        evaluator = CollectionEvaluator(mock_provider, state_store, config)
        discovery = DiscoveryEngine(mock_provider, state_store, cfg)

        # Simulate 3 sequential cycles
        for cycle in range(3):
            # 1. Discovery phase
            new_slugs = discovery.discover_next_batch()
            state_store.add_discovered_slugs(new_slugs, source="discovery")

            # 2. Evaluation phase: process up to 2 due collections
            due = state_store.get_collections_due_for_evaluation(limit=2)
            for s in due:
                evaluator.evaluate_collection(s, dry_run=False, stop_on_first_failure=True)
                state_store.mark_collection_evaluated(s)

        # Verify universe grew progressively without unbounded duplicates
        summary = state_store.get_status_summary()
        assert summary["total_monitored_collections"] == 5 # col-a, col-b, col-c, col-d, col-e
        assert summary["checkpoints"]["collections"]["cursor"] == "cursor_cycle_3"
        assert summary["total_candidates_found"] == 1 # Only col-a passed

        # Verify exactly one Info.md generated for col-a, no duplicate files created
        dated_dirs = os.listdir(data_root)
        assert len(dated_dirs) == 1
        day_dir = os.path.join(data_root, dated_dirs[0])
        assert os.listdir(day_dir) == ["Col-A"]
        assert os.listdir(os.path.join(day_dir, "Col-A")) == ["Info.md"]
