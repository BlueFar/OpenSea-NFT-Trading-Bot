import os
import shutil
import tempfile
from datetime import datetime, timezone
from src.storage.file_writer import sanitize_folder_name, write_candidate_info_md, render_info_md
from src.storage.state_store import StateStore
from src.models.collection import CollectionMetadata, Contract, Fee
from src.models.metrics import SalesMetrics, FloorPriceMetrics, ListingMetrics, DailySalesRecord
from src.models.trade import TradeEconomics, ObservedMarketData, TradeAssumptions, ModelledResults
from src.models.filters import FilterEvaluationReport, FilterCriterionResult, FilterResultStatus, DataQualityState

def test_sanitize_folder_name():
    assert sanitize_folder_name("My Project: Genesis/2") == "My-Project-Genesis-2"
    assert sanitize_folder_name("Art*Block?Collection<1>|test") == "Art-Block-Collection-1-test"
    assert sanitize_folder_name("Clean Project") == "Clean-Project"
    assert sanitize_folder_name("") == "unnamed_project"

def test_atomic_file_writer_creates_only_info_md():
    with tempfile.TemporaryDirectory() as temp_dir:
        col = CollectionMetadata(
            slug="cyber-samurai",
            name="Cyber Samurai: Origin/V1",
            created_date="2024-01-01",
            opensea_url="https://opensea.io/collection/cyber-samurai",
            safelist_status="verified",
            total_supply=5000,
            contracts=[Contract(address="0xabc", chain="ethereum")],
            fees=[Fee(fee=0.005, recipient="opensea", required=True)],
        )
        sales = SalesMetrics(
            seven_day_sales_items=7,
            seven_day_sales_transactions=7,
            average_sales_items_per_day=1.0,
            average_transactions_per_day=1.0,
            max_daily_sales_items=2,
            max_daily_transactions=2,
            min_daily_sales_items=0,
            min_daily_transactions=0,
            daily_breakdown=[DailySalesRecord(date_str="2026-09-17", sales_items=1, sales_transactions=1)],
        )
        floor = FloorPriceMetrics(current_floor=1.5, change_1d_abs_pct=2.0, change_7d_abs_pct=4.0)
        listing = ListingMetrics(total_supply=5000, listed_items=150, listed_percentage=3.0)
        econ = TradeEconomics(
            observed=ObservedMarketData(observed_top_offer=1.0, current_floor=1.5, fees_reliable=True),
            assumptions=TradeAssumptions(),
            modelled=ModelledResults(is_complete_and_reliable=True, modelled_entry_offer=1.01, target_exit_price=1.425),
        )
        report = FilterEvaluationReport(collection_slug="cyber-samurai", is_overall_pass=True)

        now_utc = datetime.now(timezone.utc)
        path = write_candidate_info_md(
            data_root=temp_dir,
            date_str="2026-09-18",
            collection=col,
            sales_metrics=sales,
            floor_metrics=floor,
            listing_metrics=listing,
            trade_economics=econ,
            filter_report=report,
            detection_dt_utc=now_utc,
            detection_dt_local=now_utc,
        )

        assert os.path.exists(path)
        assert path.endswith("Info.md")
        
        # Verify no tmp file remains
        assert not os.path.exists(path + ".tmp")
        
        # Verify Fundamentals.md was NOT created
        parent_dir = os.path.dirname(path)
        assert not os.path.exists(os.path.join(parent_dir, "Fundamentals.md"))

        # Verify content contains key sections and explicit assumptions
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "# Project: Cyber Samurai: Origin/V1" in content
            assert "## Snapshot" in content
            assert "## Collection Identity" in content
            assert "OpenSea Collection Age" in content
            assert "calculated from OpenSea created_date" in content
            assert "## Trade Economics" in content
            assert "### A. Observed Market Data (Factual / API-Sourced)" in content
            assert "### B. Model Assumptions (Configured Trading Strategy Parameters)" in content
            assert "[ASSUMPTION] Entry-Offer Premium" in content
            assert "[ASSUMPTION] Target Exit Discount" in content
            assert "[ASSUMPTION] Gas Estimate" in content
            assert "### C. Modelled Results (Theoretical Estimates)" in content
            assert "## BOT Final Result" in content
            assert "PASS" in content

def test_state_store_deduplication():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "bot.db")
        store = StateStore(db_path)

        slug = "crypto-punks"
        today = "2026-09-18"
        tomorrow = "2026-09-19"

        # Initially not recorded
        assert store.is_candidate_recorded_today(slug, today) is False

        # Record today
        store.record_candidate(slug, today, is_pass=True)
        assert store.is_candidate_recorded_today(slug, today) is True

        # Next day is not recorded yet (preserves daily candidate observations)
        assert store.is_candidate_recorded_today(slug, tomorrow) is False

        # Checkpoints
        store.save_checkpoint("collections", "cursor_123")
        assert store.get_checkpoint("collections") == "cursor_123"

def test_state_store_monitored_collections():
    """Tests the decoupled monitored collections universe scheduling pool."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "bot.db")
        store = StateStore(db_path)

        # Initially empty
        assert store.get_monitored_collection_count() == 0

        # Add discovered slugs
        store.add_discovered_slugs(["col-a", "col-b", "col-c"], source="discovery")
        assert store.get_monitored_collection_count() == 3

        # Add duplicate should be ignored
        store.add_discovered_slugs(["col-a", "col-d"], source="discovery")
        assert store.get_monitored_collection_count() == 4

        # Queue order: un-evaluated collections first
        due = store.get_collections_due_for_evaluation(limit=2)
        assert len(due) == 2

        # Mark one as evaluated
        store.mark_collection_evaluated(due[0])

        # Next fetch should prioritize remaining un-evaluated
        next_due = store.get_collections_due_for_evaluation(limit=2)
        assert due[0] not in next_due



def test_info_md_never_overwrites_another_collection_with_the_same_folder_name():
    with tempfile.TemporaryDirectory() as temp_dir:
        def write(slug, name):
            col = CollectionMetadata(slug=slug, name=name, contracts=[Contract(address="0xabc", chain="ethereum")])
            now = datetime.now(timezone.utc)
            return write_candidate_info_md(
                data_root=temp_dir, date_str="2026-10-09", collection=col,
                sales_metrics=SalesMetrics(0, 0, 0.0, 0.0, 0, 0, 0, 0, daily_breakdown=[]), floor_metrics=FloorPriceMetrics(current_floor=1.0),
                listing_metrics=ListingMetrics(), trade_economics=TradeEconomics(
                    observed=ObservedMarketData(), assumptions=TradeAssumptions(), modelled=ModelledResults()),
                filter_report=FilterEvaluationReport(collection_slug=slug, is_overall_pass=True),
                detection_dt_utc=now, detection_dt_local=now)

        first = write("pixels-farm", "Pixels - Farm Land")
        second = write("pixels-farm-land-ronin", "Pixels Farm Land")
        assert first != second and os.path.exists(first) and os.path.exists(second)
        assert os.path.basename(os.path.dirname(second)) == "Pixels-Farm-Land-pixels-farm-land-ronin"
        assert "Collection Slug**: pixels-farm\n" in open(first, encoding="utf-8").read()
        # Re-checks later the same day still land in each collection's own folder
        assert write("pixels-farm", "Pixels - Farm Land") == first
        assert write("pixels-farm-land-ronin", "Pixels Farm Land") == second
