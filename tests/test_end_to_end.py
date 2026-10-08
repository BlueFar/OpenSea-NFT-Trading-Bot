import os
import tempfile
import pytest
from unittest.mock import MagicMock
from src.config.settings import BotConfig, GeneralConfig, FiltersConfig, TradingFrequencyFilterConfig, FloorChangeFilterConfig, ListedItemsFilterConfig, VerificationFilterConfig, ProjectAgeFilterConfig
from src.collectors.orchestrator import CollectionEvaluator
from src.storage.state_store import StateStore
from tests.mock_data import make_sample_collection, make_sample_sales_events, make_sample_floor_history
from src.models.collection import Offer, CollectionStats

def test_full_evaluation_passing_collection_creates_info_md():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_root = os.path.join(temp_dir, "data")
        db_path = os.path.join(temp_dir, "state", "bot.db")

        config = BotConfig(
            general=GeneralConfig(data_root=data_root, state_db_path=db_path),
            filters=FiltersConfig(
                trading_frequency=TradingFrequencyFilterConfig(max_threshold=2.0, metric="average_daily_sales"),
                floor_change_1d=FloorChangeFilterConfig(max_change_pct=8.0),
                floor_change_7d=FloorChangeFilterConfig(max_change_pct=10.0),
                listed_items=ListedItemsFilterConfig(max_listed_pct=6.0),
                verification=VerificationFilterConfig(required_status=["verified"]),
                project_age=ProjectAgeFilterConfig(min_age_days=60.0),
            )
        )
        state_store = StateStore(db_path)

        # Mock provider
        mock_provider = MagicMock()
        mock_provider.get_collection.return_value = make_sample_collection(
            slug="passing-collection",
            name="Passing Collection",
            created_days_ago=100.0,
            safelist_status="verified",
            total_supply=10000,
        )
        mock_provider.get_active_listings_count.return_value = (300, False) # 3% (< 6%)
        mock_provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.45, offer_price=0.5) # 1.0/day (<= 2)
        
        curr_floor, points_1d, points_7d = make_sample_floor_history(1.5, pct_change_1d=2.0, pct_change_7d=3.0)
        mock_provider.get_collection_stats.return_value = CollectionStats(floor_price=curr_floor, floor_price_symbol="ETH")
        mock_provider.get_floor_price_history.side_effect = lambda slug, timeframe: points_1d if timeframe == "one_day" else points_7d
        mock_provider.get_top_offer.return_value = Offer(order_hash="0xoff", chain="ethereum", price_value=0.9, price_currency="WETH")

        evaluator = CollectionEvaluator(mock_provider, state_store, config)

        # 1. Run evaluation
        report = evaluator.evaluate_collection("passing-collection", dry_run=False)
        assert report is not None
        assert report.is_overall_pass is True

        # Check file was created
        daily_folders = os.listdir(data_root)
        assert len(daily_folders) == 1
        day_dir = os.path.join(data_root, daily_folders[0])
        project_dirs = os.listdir(day_dir)
        assert "Passing-Collection" in project_dirs

        info_path = os.path.join(day_dir, "Passing-Collection", "Info.md")
        assert os.path.exists(info_path)
        assert not os.path.exists(os.path.join(day_dir, "Passing-Collection", "Fundamentals.md"))

        # 2. Run evaluation second time on same date -> should NOT create duplicate folder
        report2 = evaluator.evaluate_collection("passing-collection", dry_run=False)
        assert report2 is not None
        # Directory count must still be 1 (no Passing-Collection-2)
        assert len(os.listdir(day_dir)) == 1

def test_evaluation_rejection_creates_no_folders():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_root = os.path.join(temp_dir, "data")
        db_path = os.path.join(temp_dir, "state", "bot.db")
        config = BotConfig(general=GeneralConfig(data_root=data_root, state_db_path=db_path))
        state_store = StateStore(db_path)

        # Unverified collection
        mock_provider = MagicMock()
        mock_provider.get_collection.return_value = make_sample_collection(safelist_status="approved") # Not verified

        evaluator = CollectionEvaluator(mock_provider, state_store, config)
        report = evaluator.evaluate_collection("unverified-collection", dry_run=False)

        # Should be rejected
        assert report is None
        # Data root must not exist or be empty
        if os.path.exists(data_root):
            assert len(os.listdir(data_root)) == 0
