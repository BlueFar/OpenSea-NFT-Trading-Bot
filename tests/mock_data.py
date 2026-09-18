from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from src.models.collection import (
    CollectionMetadata,
    Contract,
    Fee,
    CollectionStats,
    FloorPricePoint,
    SaleEvent,
    Listing,
    Offer,
)

def make_sample_collection(
    slug: str = "passing-collection",
    name: str = "Passing Collection",
    created_days_ago: float = 120.0,
    safelist_status: str = "verified",
    total_supply: int = 5000,
    mp_fee: float = 0.005,  # 0.5%
    royalty_fee: float = 0.05,  # 5.0%
) -> CollectionMetadata:
    now_dt = datetime.now(timezone.utc)
    created_dt = now_dt - timedelta(days=created_days_ago)
    created_date_str = created_dt.strftime("%Y-%m-%d")

    fees = []
    if mp_fee is not None:
        fees.append(Fee(fee=mp_fee, recipient="opensea", required=True))
    if royalty_fee is not None:
        fees.append(Fee(fee=royalty_fee, recipient="0xcreator", required=True))

    return CollectionMetadata(
        slug=slug,
        name=name,
        description="A verified sample NFT collection",
        created_date=created_date_str,
        opensea_url=f"https://opensea.io/collection/{slug}",
        project_url="https://example.com",
        twitter_username="example_nft",
        discord_url="https://discord.gg/example",
        safelist_status=safelist_status,
        is_disabled=False,
        is_nsfw=False,
        total_supply=total_supply,
        contracts=[Contract(address="0x1234567890abcdef1234567890abcdef12345678", chain="ethereum")],
        fees=fees,
    )

def make_sample_sales_events(
    daily_sales: List[int],
    tz_name: str = "Asia/Kolkata",
) -> List[SaleEvent]:
    """
    Generates sale events for the last 7 complete calendar days.
    daily_sales: list of 7 ints representing sales count per day (oldest to newest).
    """
    from src.utils.time import get_seven_complete_calendar_days, get_calendar_day_utc_bounds

    days = get_seven_complete_calendar_days(tz_name)
    events: List[SaleEvent] = []

    for day, count in zip(days, daily_sales):
        start_ts, end_ts = get_calendar_day_utc_bounds(day, tz_name)
        mid_ts = (start_ts + end_ts) // 2
        for i in range(count):
            events.append(SaleEvent(
                event_id=f"eth:tx_{day}_{i}:0xcontract:token_{i}:{mid_ts + i * 10}",
                chain="ethereum",
                event_timestamp=mid_ts + i * 10,
                transaction=f"0xtx_{day}_{i}",
                order_hash=f"0xorder_{day}_{i}",
                contract_address="0xcontract",
                token_id=f"token_{i}",
                quantity=1,
                seller="0xseller",
                buyer="0xbuyer",
                price_value=1.2,
                price_currency="ETH",
            ))
    return events

def make_sample_floor_history(
    current_floor: float = 1.0,
    pct_change_1d: float = 0.0,
    pct_change_7d: float = 0.0,
) -> tuple:
    """Returns (current_floor, floor_1d_points, floor_7d_points)."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    
    # floor_1d_ago * (1 + pct/100) = current_floor => floor_1d_ago = current_floor / (1 + pct/100)
    floor_1d_ago = current_floor / (1.0 + (pct_change_1d / 100.0))
    floor_7d_ago = current_floor / (1.0 + (pct_change_7d / 100.0))

    points_1d = [
        FloorPricePoint(time=now_ts - 86400, token_unit=floor_1d_ago, symbol="ETH"),
        FloorPricePoint(time=now_ts, token_unit=current_floor, symbol="ETH"),
    ]
    points_7d = [
        FloorPricePoint(time=now_ts - 7 * 86400, token_unit=floor_7d_ago, symbol="ETH"),
        FloorPricePoint(time=now_ts, token_unit=current_floor, symbol="ETH"),
    ]
    return current_floor, points_1d, points_7d
