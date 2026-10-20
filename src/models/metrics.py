from dataclasses import dataclass, field
from typing import Any, List, Optional
from .filters import DataQualityState

@dataclass
class DailySalesRecord:
    date_str: str  # YYYY-MM-DD in local configured timezone
    sales_items: int
    sales_transactions: int
    is_complete_day: bool = True

@dataclass
class SalesMetrics:
    seven_day_sales_items: int
    seven_day_sales_transactions: int
    average_sales_items_per_day: float
    average_transactions_per_day: float
    max_daily_sales_items: int
    max_daily_transactions: int
    min_daily_sales_items: int
    min_daily_transactions: int
    daily_breakdown: List[DailySalesRecord] = field(default_factory=list)
    today_in_progress_record: Optional[DailySalesRecord] = None
    data_quality: DataQualityState = DataQualityState.AVAILABLE
    floor_sales: Optional[Any] = None
    offer_sales: Optional[Any] = None  # OfferSalesMetrics once the floor is known (src/metrics/floor_sales.py)

@dataclass
class FloorPriceMetrics:
    current_floor: Optional[float] = None
    floor_currency: str = "ETH"
    floor_1d_ago: Optional[float] = None
    change_1d_signed_pct: Optional[float] = None
    change_1d_abs_pct: Optional[float] = None
    floor_7d_ago: Optional[float] = None
    change_7d_signed_pct: Optional[float] = None
    change_7d_abs_pct: Optional[float] = None
    data_quality: DataQualityState = DataQualityState.AVAILABLE

@dataclass
class ListingMetrics:
    total_supply: Optional[int] = None
    listed_items: Optional[int] = None
    listed_percentage: Optional[float] = None
    is_early_exit_exceeded: bool = False
    data_quality: DataQualityState = DataQualityState.AVAILABLE
