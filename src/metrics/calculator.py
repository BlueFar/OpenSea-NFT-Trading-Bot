from typing import List, Optional, Dict, Tuple
from datetime import datetime, date, timezone
from ..models.collection import SaleEvent, FloorPricePoint
from ..models.metrics import DailySalesRecord, SalesMetrics, FloorPriceMetrics, ListingMetrics
from ..models.filters import DataQualityState
from ..utils.time import (
    get_seven_complete_calendar_days,
    get_calendar_day_utc_bounds,
    now_local,
    get_timezone,
)

def deduplicate_sale_events(events: List[SaleEvent]) -> List[SaleEvent]:
    """
    Deduplicates sale events based on stable event composite key.
    Prevents double-counting from overlapping queries, retries, or pagination.
    """
    seen_keys = set()
    deduped = []
    for ev in events:
        key = ev.event_id or f"{ev.chain}:{ev.transaction or ev.order_hash}:{ev.contract_address}:{ev.token_id}:{ev.event_timestamp}"
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(ev)
    return deduped

def compute_sales_metrics(
    events: Optional[List[SaleEvent]],
    tz_name: str = "Asia/Kolkata",
    has_sufficient_history: bool = True,
) -> SalesMetrics:
    """
    Calculates 7 complete calendar days sales metrics in the configured timezone.
    Strictly partitions by local midnight-to-midnight.
    """
    if events is None or not has_sufficient_history:
        return SalesMetrics(
            seven_day_sales_items=0,
            seven_day_sales_transactions=0,
            average_sales_items_per_day=0.0,
            average_transactions_per_day=0.0,
            max_daily_sales_items=0,
            max_daily_transactions=0,
            min_daily_sales_items=0,
            min_daily_transactions=0,
            daily_breakdown=[],
            data_quality=DataQualityState.INSUFFICIENT_HISTORY,
        )

    deduped = deduplicate_sale_events(events)
    seven_days = get_seven_complete_calendar_days(tz_name)
    daily_records: List[DailySalesRecord] = []

    # Map events into the 7 completed calendar days
    for cal_day in seven_days:
        start_ts, end_ts = get_calendar_day_utc_bounds(cal_day, tz_name)
        day_events = [ev for ev in deduped if start_ts <= ev.event_timestamp <= end_ts]
        
        # Transaction count = distinct transaction or order hashes
        tx_hashes = set()
        items_count = 0
        for ev in day_events:
            tx_key = ev.transaction or ev.order_hash or ev.event_id
            tx_hashes.add(tx_key)
            items_count += max(1, ev.quantity)
        
        daily_records.append(DailySalesRecord(
            date_str=cal_day.strftime("%Y-%m-%d"),
            sales_items=items_count,
            sales_transactions=len(tx_hashes),
            is_complete_day=True,
        ))

    # In-progress day (today)
    today_date = now_local(tz_name).date()
    today_start_ts, today_end_ts = get_calendar_day_utc_bounds(today_date, tz_name)
    today_events = [ev for ev in deduped if today_start_ts <= ev.event_timestamp <= today_end_ts]
    today_tx_hashes = set(ev.transaction or ev.order_hash or ev.event_id for ev in today_events)
    today_record = DailySalesRecord(
        date_str=today_date.strftime("%Y-%m-%d"),
        sales_items=sum(max(1, ev.quantity) for ev in today_events),
        sales_transactions=len(today_tx_hashes),
        is_complete_day=False,
    )

    # 7-day aggregates across completed days
    seven_day_items = sum(r.sales_items for r in daily_records)
    seven_day_txs = sum(r.sales_transactions for r in daily_records)
    avg_items = seven_day_items / 7.0
    avg_txs = seven_day_txs / 7.0
    max_items = max((r.sales_items for r in daily_records), default=0)
    max_txs = max((r.sales_transactions for r in daily_records), default=0)
    min_items = min((r.sales_items for r in daily_records), default=0)
    min_txs = min((r.sales_transactions for r in daily_records), default=0)

    return SalesMetrics(
        seven_day_sales_items=seven_day_items,
        seven_day_sales_transactions=seven_day_txs,
        average_sales_items_per_day=avg_items,
        average_transactions_per_day=avg_txs,
        max_daily_sales_items=max_items,
        max_daily_transactions=max_txs,
        min_daily_sales_items=min_items,
        min_daily_transactions=min_txs,
        daily_breakdown=daily_records,
        today_in_progress_record=today_record,
        data_quality=DataQualityState.AVAILABLE,
    )

def compute_floor_metrics(
    current_floor: Optional[float],
    floor_points_1d: Optional[List[FloorPricePoint]],
    floor_points_7d: Optional[List[FloorPricePoint]],
    currency: str = "ETH",
) -> FloorPriceMetrics:
    """Calculates 1-day and 7-day percentage changes in floor price."""
    if current_floor is None:
        return FloorPriceMetrics(
            current_floor=None,
            floor_currency=currency,
            data_quality=DataQualityState.MISSING,
        )

    floor_1d_ago = None
    if floor_points_1d and len(floor_points_1d) > 0:
        # Sort by timestamp ascending, take earliest point in the 1-day timeframe
        sorted_1d = sorted(floor_points_1d, key=lambda x: x.time)
        floor_1d_ago = sorted_1d[0].token_unit

    floor_7d_ago = None
    if floor_points_7d and len(floor_points_7d) > 0:
        sorted_7d = sorted(floor_points_7d, key=lambda x: x.time)
        floor_7d_ago = sorted_7d[0].token_unit

    change_1d_signed = None
    change_1d_abs = None
    if floor_1d_ago is not None and floor_1d_ago > 0:
        change_1d_signed = ((current_floor - floor_1d_ago) / floor_1d_ago) * 100.0
        change_1d_abs = abs(change_1d_signed)

    change_7d_signed = None
    change_7d_abs = None
    if floor_7d_ago is not None and floor_7d_ago > 0:
        change_7d_signed = ((current_floor - floor_7d_ago) / floor_7d_ago) * 100.0
        change_7d_abs = abs(change_7d_signed)

    dq = DataQualityState.AVAILABLE
    if floor_1d_ago is None or floor_7d_ago is None:
        dq = DataQualityState.INSUFFICIENT_HISTORY

    return FloorPriceMetrics(
        current_floor=current_floor,
        floor_currency=currency,
        floor_1d_ago=floor_1d_ago,
        change_1d_signed_pct=change_1d_signed,
        change_1d_abs_pct=change_1d_abs,
        floor_7d_ago=floor_7d_ago,
        change_7d_signed_pct=change_7d_signed,
        change_7d_abs_pct=change_7d_abs,
        data_quality=dq,
    )

def compute_listing_metrics(
    total_supply: Optional[int],
    listed_items: Optional[int],
    is_early_exit: bool = False,
) -> ListingMetrics:
    """Calculates active listing percentage relative to total supply."""
    if total_supply is None or total_supply <= 0:
        return ListingMetrics(
            total_supply=total_supply,
            listed_items=listed_items,
            listed_percentage=None,
            is_early_exit_exceeded=is_early_exit,
            data_quality=DataQualityState.MISSING,
        )

    if listed_items is None:
        return ListingMetrics(
            total_supply=total_supply,
            listed_items=None,
            listed_percentage=None,
            is_early_exit_exceeded=is_early_exit,
            data_quality=DataQualityState.MISSING,
        )

    pct = (listed_items / total_supply) * 100.0
    return ListingMetrics(
        total_supply=total_supply,
        listed_items=listed_items,
        listed_percentage=pct,
        is_early_exit_exceeded=is_early_exit,
        data_quality=DataQualityState.AVAILABLE,
    )
