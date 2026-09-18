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

        # Verify content contains key sections
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "# Project: Cyber Samurai: Origin/V1" in content
            assert "## Snapshot" in content
            assert "## Collection Identity" in content
            assert "## Trade Economics" in content
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
