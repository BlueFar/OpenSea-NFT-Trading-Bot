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


def test_failing_a_later_check_moves_its_info_md_out_and_passing_again_writes_a_new_one():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_root = os.path.join(temp_dir, "data")
        removed_root = os.path.join(temp_dir, "data_removed")
        db_path = os.path.join(temp_dir, "state", "bot.db")
        config = BotConfig(
            general=GeneralConfig(data_root=data_root, removed_root=removed_root, state_db_path=db_path),
            filters=FiltersConfig(
                trading_frequency=TradingFrequencyFilterConfig(max_threshold=2.0, metric="average_daily_sales"),
                floor_change_1d=FloorChangeFilterConfig(max_change_pct=8.0),
                floor_change_7d=FloorChangeFilterConfig(max_change_pct=10.0),
                listed_items=ListedItemsFilterConfig(max_listed_pct=6.0),
                verification=VerificationFilterConfig(required_status=["verified"]),
                project_age=ProjectAgeFilterConfig(min_age_days=60.0),
            )
        )
        store = StateStore(db_path)
        provider = MagicMock()
        provider.get_collection.return_value = make_sample_collection(
            slug="passing-collection", name="Passing Collection", created_days_ago=100.0,
            safelist_status="verified", total_supply=10000)
        provider.get_active_listings_count.return_value = (300, False)
        provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.45, offer_price=0.5)
        curr_floor, points_1d, points_7d = make_sample_floor_history(1.5, pct_change_1d=2.0, pct_change_7d=3.0)
        provider.get_collection_stats.return_value = CollectionStats(floor_price=curr_floor, floor_price_symbol="ETH")
        provider.get_floor_price_history.side_effect = lambda slug, timeframe: points_1d if timeframe == "one_day" else points_7d
        provider.get_top_offer.return_value = Offer(order_hash="0xoff", chain="ethereum", price_value=0.9, price_currency="WETH")
        store.add_discovered_slugs(["passing-collection"])
        evaluator = CollectionEvaluator(provider, store, config)

        assert evaluator.evaluate_collection("passing-collection").is_overall_pass
        day = os.listdir(data_root)[0]
        info = os.path.join(data_root, day, "Passing-Collection", "Info.md")
        assert os.path.exists(info)
        # Stage 2's notes travel with the folder
        open(os.path.join(os.path.dirname(info), "Fundamentals.md"), "w").close()

        # Too many listed now: fails early, folder leaves data_root
        provider.get_active_listings_count.return_value = (900, False)
        evaluator.evaluate_collection("passing-collection")
        assert not os.path.exists(os.path.dirname(info))
        moved = os.path.join(removed_root, day, "Passing-Collection")
        assert os.path.exists(os.path.join(moved, "Info.md")) and os.path.exists(os.path.join(moved, "Fundamentals.md"))
        assert any(e["kind"] == "candidate_removed" for e in store.get_recent_events(10))

        # Passes again the same day: a fresh Info.md is written although the day's pass is already recorded
        provider.get_active_listings_count.return_value = (300, False)
        assert evaluator.evaluate_collection("passing-collection").is_overall_pass
        assert os.path.exists(info)

        # Nothing else is touched: a sweep with the latest check passing removes nothing
        assert evaluator.remove_all_failing_dossiers() == 0

        # Dry runs never move files
        provider.get_active_listings_count.return_value = (900, False)
        evaluator.evaluate_collection("passing-collection", dry_run=True)
        assert os.path.exists(info)
        # ...but the startup sweep does, for a collection whose latest check failed
        assert evaluator.remove_all_failing_dossiers() == 1
        assert not os.path.exists(info) and os.path.exists(os.path.join(removed_root, day, "Passing-Collection-2", "Info.md"))


def test_removal_never_touches_a_folder_holding_another_collections_info_md(tmp_path):
    from src.storage.file_writer import remove_candidate_dossier
    d = tmp_path / "data" / "2026-10-09" / "Pixels-Farm-Land"
    d.mkdir(parents=True)
    (d / "Info.md").write_text("- **Collection Slug**: pixels-farm-land-ronin\n", encoding="utf-8")
    assert remove_candidate_dossier(str(d / "Info.md"), "pixels-farm", str(tmp_path / "data"), str(tmp_path / "out")) is None
    assert (d / "Info.md").exists()
    # Outside data_root: ignored
    assert remove_candidate_dossier(str(tmp_path / "Info.md"), "x", str(tmp_path / "data")) is None
    # Empty removed_root deletes
    assert remove_candidate_dossier(str(d / "Info.md"), "pixels-farm-land-ronin", str(tmp_path / "data"), "") == ""
    assert not d.exists()
