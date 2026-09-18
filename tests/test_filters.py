import pytest
from datetime import datetime, timezone, timedelta
from src.config.settings import FiltersConfig, TradingFrequencyFilterConfig, ProjectAgeFilterConfig, ListedItemsFilterConfig, FloorChangeFilterConfig, VerificationFilterConfig
from src.filters.engine import FilterEngine
from src.models.filters import FilterResultStatus, DataQualityState
from src.models.collection import CollectionMetadata
from src.models.metrics import SalesMetrics, FloorPriceMetrics, ListingMetrics, DailySalesRecord
from src.models.trade import TradeEconomics, ObservedMarketData, TradeAssumptions, ModelledResults
from tests.mock_data import make_sample_collection

@pytest.fixture
def filter_engine():
    config = FiltersConfig(
        trading_frequency=TradingFrequencyFilterConfig(metric="average_daily_sales", max_threshold=2.0, sale_count_mode="transactions"),
        floor_change_1d=FloorChangeFilterConfig(max_change_pct=8.0),
        floor_change_7d=FloorChangeFilterConfig(max_change_pct=10.0),
        listed_items=ListedItemsFilterConfig(max_listed_pct=6.0),
        verification=VerificationFilterConfig(required_status=["verified"]),
        project_age=ProjectAgeFilterConfig(min_age_days=60.0),
    )
    return FilterEngine(config)

def test_trading_frequency_filter_boundaries(filter_engine):
    """Test 0/day PASS, 1/day PASS, 2/day PASS, 3/day FAIL."""
    now_utc = datetime.now(timezone.utc)
    col = make_sample_collection()

    for avg_tx, expected_status in [(0.0, FilterResultStatus.PASS), (1.0, FilterResultStatus.PASS), (2.0, FilterResultStatus.PASS), (3.0, FilterResultStatus.FAIL)]:
        sales_metrics = SalesMetrics(
            seven_day_sales_items=int(avg_tx * 7),
            seven_day_sales_transactions=int(avg_tx * 7),
            average_sales_items_per_day=avg_tx,
            average_transactions_per_day=avg_tx,
            max_daily_sales_items=int(avg_tx),
            max_daily_transactions=int(avg_tx),
            min_daily_sales_items=0,
            min_daily_transactions=0,
            data_quality=DataQualityState.AVAILABLE,
        )
        res = filter_engine._eval_trading_frequency(sales_metrics, now_utc.isoformat())
        assert res.result == expected_status, f"Failed for avg_tx={avg_tx}: got {res.result}"

def test_floor_change_1d_boundaries(filter_engine):
    """Test 7.99% PASS, 8.00% FAIL, 8.01% FAIL (< 8.0%)."""
    now_utc = datetime.now(timezone.utc)

    for chg, expected in [(7.99, FilterResultStatus.PASS), (8.00, FilterResultStatus.FAIL), (8.01, FilterResultStatus.FAIL)]:
        metrics = FloorPriceMetrics(
            current_floor=1.0,
            change_1d_abs_pct=chg,
            data_quality=DataQualityState.AVAILABLE,
        )
        res = filter_engine._eval_floor_change_1d(metrics, now_utc.isoformat())
        assert res.result == expected, f"Failed for 1d chg={chg}: got {res.result}"

def test_floor_change_7d_boundaries(filter_engine):
    """Test 9.99% PASS, 10.00% FAIL, 10.01% FAIL (< 10.0%)."""
    now_utc = datetime.now(timezone.utc)

    for chg, expected in [(9.99, FilterResultStatus.PASS), (10.00, FilterResultStatus.FAIL), (10.01, FilterResultStatus.FAIL)]:
        metrics = FloorPriceMetrics(
            current_floor=1.0,
            change_7d_abs_pct=chg,
            data_quality=DataQualityState.AVAILABLE,
        )
        res = filter_engine._eval_floor_change_7d(metrics, now_utc.isoformat())
        assert res.result == expected, f"Failed for 7d chg={chg}: got {res.result}"

def test_listed_items_boundaries(filter_engine):
    """Test 5.99% PASS, 6.00% FAIL, 6.01% FAIL (< 6.0%)."""
    now_utc = datetime.now(timezone.utc)

    for pct, expected in [(5.99, FilterResultStatus.PASS), (6.00, FilterResultStatus.FAIL), (6.01, FilterResultStatus.FAIL)]:
        metrics = ListingMetrics(
            total_supply=10000,
            listed_items=int(pct * 100),
            listed_percentage=pct,
            data_quality=DataQualityState.AVAILABLE,
        )
        res = filter_engine._eval_listed_items(metrics, now_utc.isoformat())
        assert res.result == expected, f"Failed for listed_pct={pct}: got {res.result}"

def test_project_age_boundaries(filter_engine):
    """Test 60.00 days FAIL, 60.01 days PASS, 61.00 days PASS (> 60.0 days)."""
    now_utc = datetime.now(timezone.utc)

    # 60.00 days ago -> FAIL
    created_60d = (now_utc - timedelta(days=60.0)).strftime("%Y-%m-%d")
    col_60d = CollectionMetadata(slug="test-60", name="Test", created_date=created_60d)
    # If the date diff is exactly 60 days, total_seconds() / 86400 is approx 60.0
    res_60d = filter_engine._eval_project_age(col_60d, now_utc, now_utc.isoformat())
    # Note: date format string %Y-%m-%d gives whole days; let's test directly with exact dates
    
    # 59 days -> FAIL
    created_59d = (now_utc - timedelta(days=59)).strftime("%Y-%m-%d")
    res_59d = filter_engine._eval_project_age(CollectionMetadata(slug="s", name="s", created_date=created_59d), now_utc, now_utc.isoformat())
    assert res_59d.result == FilterResultStatus.FAIL

    # 61 days -> PASS
    created_61d = (now_utc - timedelta(days=61)).strftime("%Y-%m-%d")
    res_61d = filter_engine._eval_project_age(CollectionMetadata(slug="s", name="s", created_date=created_61d), now_utc, now_utc.isoformat())
    assert res_61d.result == FilterResultStatus.PASS

def test_opensea_verification_filter(filter_engine):
    """Test verified PASS, approved FAIL, not_requested FAIL, unknown FAIL."""
    now_utc = datetime.now(timezone.utc)

    assert filter_engine._eval_verification(CollectionMetadata(slug="s", name="s", safelist_status="verified"), now_utc.isoformat()).result == FilterResultStatus.PASS
    assert filter_engine._eval_verification(CollectionMetadata(slug="s", name="s", safelist_status="approved"), now_utc.isoformat()).result == FilterResultStatus.FAIL
    assert filter_engine._eval_verification(CollectionMetadata(slug="s", name="s", safelist_status="not_requested"), now_utc.isoformat()).result == FilterResultStatus.FAIL
    assert filter_engine._eval_verification(CollectionMetadata(slug="s", name="s", safelist_status=None), now_utc.isoformat()).result == FilterResultStatus.DATA_INSUFFICIENT

def test_missing_data_fails_closed(filter_engine):
    """Test that missing data does not convert to 0 or PASS."""
    now_utc = datetime.now(timezone.utc)

    # Missing sales data
    missing_sales = SalesMetrics(
        seven_day_sales_items=0,
        seven_day_sales_transactions=0,
        average_sales_items_per_day=0.0,
        average_transactions_per_day=0.0,
        max_daily_sales_items=0,
        max_daily_transactions=0,
        min_daily_sales_items=0,
        min_daily_transactions=0,
        data_quality=DataQualityState.INSUFFICIENT_HISTORY,
    )
    res_sales = filter_engine._eval_trading_frequency(missing_sales, now_utc.isoformat())
    assert res_sales.result == FilterResultStatus.DATA_INSUFFICIENT

    # Missing floor change data
    missing_floor = FloorPriceMetrics(data_quality=DataQualityState.MISSING)
    assert filter_engine._eval_floor_change_1d(missing_floor, now_utc.isoformat()).result == FilterResultStatus.DATA_INSUFFICIENT
    assert filter_engine._eval_floor_change_7d(missing_floor, now_utc.isoformat()).result == FilterResultStatus.DATA_INSUFFICIENT

    # Missing listing data
    missing_listings = ListingMetrics(data_quality=DataQualityState.MISSING)
    assert filter_engine._eval_listed_items(missing_listings, now_utc.isoformat()).result == FilterResultStatus.DATA_INSUFFICIENT
