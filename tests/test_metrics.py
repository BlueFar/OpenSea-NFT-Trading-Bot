import pytest
from datetime import datetime, timezone
from src.metrics.calculator import (
    deduplicate_sale_events,
    compute_sales_metrics,
    compute_floor_metrics,
    compute_listing_metrics,
)
from src.models.collection import SaleEvent, FloorPricePoint
from src.models.filters import DataQualityState
from tests.mock_data import make_sample_sales_events, make_sample_floor_history

def test_deduplicate_sale_events():
    """Validates that duplicate sale events from retries/pagination are removed."""
    ev1 = SaleEvent(
        event_id="eth:tx1:0xcontract:1:1000",
        chain="ethereum",
        event_timestamp=1000,
        transaction="tx1",
        order_hash="0xorder1",
        contract_address="0xcontract",
        token_id="1",
        quantity=1,
    )
    # Duplicate with same event_id
    ev2 = SaleEvent(
        event_id="eth:tx1:0xcontract:1:1000",
        chain="ethereum",
        event_timestamp=1000,
        transaction="tx1",
        order_hash="0xorder1",
        contract_address="0xcontract",
        token_id="1",
        quantity=1,
    )
    # Distinct event
    ev3 = SaleEvent(
        event_id="eth:tx2:0xcontract:2:2000",
        chain="ethereum",
        event_timestamp=2000,
        transaction="tx2",
        order_hash="0xorder2",
        contract_address="0xcontract",
        token_id="2",
        quantity=1,
    )

    deduped = deduplicate_sale_events([ev1, ev2, ev3])
    assert len(deduped) == 2
    assert deduped[0].event_id == "eth:tx1:0xcontract:1:1000"
    assert deduped[1].event_id == "eth:tx2:0xcontract:2:2000"

def test_sales_metrics_calendar_day_grouping():
    """Validates grouping across 7 complete calendar days in Asia/Kolkata."""
    # 7 daily sales counts: [1, 2, 0, 1, 2, 1, 0] -> Total = 7, Avg = 1.0, Max = 2
    events = make_sample_sales_events([1, 2, 0, 1, 2, 1, 0], tz_name="Asia/Kolkata")
    
    metrics = compute_sales_metrics(events, tz_name="Asia/Kolkata")
    assert metrics.data_quality == DataQualityState.AVAILABLE
    assert len(metrics.daily_breakdown) == 7
    assert metrics.seven_day_sales_transactions == 7
    assert pytest.approx(metrics.average_transactions_per_day, rel=1e-4) == 1.0
    assert metrics.max_daily_transactions == 2
    assert metrics.min_daily_transactions == 0

def test_floor_price_change_precision_and_sign():
    """Validates signed change vs absolute change."""
    # Current floor = 1.20, Floor 24h ago = 1.16 (+3.448%)
    points_1d = [
        FloorPricePoint(time=1000, token_unit=1.16, symbol="ETH"),
        FloorPricePoint(time=2000, token_unit=1.20, symbol="ETH"),
    ]
    # Current floor = 1.20, Floor 7d ago = 1.30 (-7.692%)
    points_7d = [
        FloorPricePoint(time=500, token_unit=1.30, symbol="ETH"),
        FloorPricePoint(time=2000, token_unit=1.20, symbol="ETH"),
    ]

    metrics = compute_floor_metrics(
        current_floor=1.20,
        floor_points_1d=points_1d,
        floor_points_7d=points_7d,
    )

    # 1-Day: (1.20 - 1.16)/1.16 * 100 = +3.448%
    assert metrics.change_1d_signed_pct > 0
    assert pytest.approx(metrics.change_1d_signed_pct, rel=1e-3) == 3.448
    assert pytest.approx(metrics.change_1d_abs_pct, rel=1e-3) == 3.448

    # 7-Day: (1.20 - 1.30)/1.30 * 100 = -7.692%
    assert metrics.change_7d_signed_pct < 0
    assert pytest.approx(metrics.change_7d_signed_pct, rel=1e-3) == -7.692
    assert pytest.approx(metrics.change_7d_abs_pct, rel=1e-3) == 7.692
