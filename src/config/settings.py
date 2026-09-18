import os
from typing import List, Optional
import yaml
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

class GeneralConfig(BaseModel):
    bot_timezone: str = "Asia/Kolkata"
    data_root: str = "./data"
    state_db_path: str = "./state/bot.db"
    log_level: str = "INFO"

class DiscoveryConfig(BaseModel):
    primary_source: str = "collections"
    batch_size: int = 50
    max_pages_per_cycle: int = 2
    enable_top: bool = True
    enable_trending: bool = True
    chains: List[str] = Field(default_factory=lambda: ["ethereum", "base", "polygon"])
    watchlist: List[str] = Field(default_factory=list)

class TradingFrequencyFilterConfig(BaseModel):
    metric: str = "average_daily_sales"  # Options: average_daily_sales, max_daily_sales, both
    max_threshold: float = 2.0
    sale_count_mode: str = "transactions"  # Options: transactions, item_quantity

class FloorChangeFilterConfig(BaseModel):
    max_change_pct: float

class OfferToFloorFilterConfig(BaseModel):
    enabled: bool = False  # Kept disabled/advisory pending trading formula confirmation
    min_ratio_pct: float = 40.0
    formula: str = "modelled_entry_offer_to_floor"

class ListedItemsFilterConfig(BaseModel):
    max_listed_pct: float = 6.0

class VerificationFilterConfig(BaseModel):
    required_status: List[str] = Field(default_factory=lambda: ["verified"])

class ProjectAgeFilterConfig(BaseModel):
    min_age_days: float = 60.0

class FiltersConfig(BaseModel):
    trading_frequency: TradingFrequencyFilterConfig = Field(default_factory=TradingFrequencyFilterConfig)
    floor_change_1d: FloorChangeFilterConfig = Field(default_factory=lambda: FloorChangeFilterConfig(max_change_pct=8.0))
    floor_change_7d: FloorChangeFilterConfig = Field(default_factory=lambda: FloorChangeFilterConfig(max_change_pct=10.0))
    offer_to_floor: OfferToFloorFilterConfig = Field(default_factory=OfferToFloorFilterConfig)
    listed_items: ListedItemsFilterConfig = Field(default_factory=ListedItemsFilterConfig)
    verification: VerificationFilterConfig = Field(default_factory=VerificationFilterConfig)
    project_age: ProjectAgeFilterConfig = Field(default_factory=ProjectAgeFilterConfig)

class TradeModelConfig(BaseModel):
    entry_offer_premium_pct: float = 1.0
    target_sale_discount_from_floor_pct: float = 5.0
    gas_estimate_eth: float = 0.005

class SchedulerConfig(BaseModel):
    discovery_interval_seconds: int = 3600
    candidate_refresh_interval_seconds: int = 300
    request_delay_seconds: float = 0.5
    rate_limit_backoff_factor: float = 2.0
    max_retries: int = 5

class BotConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    trade_model: TradeModelConfig = Field(default_factory=TradeModelConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    opensea_api_key: Optional[str] = None

def load_config(config_path: str = "config/config.yaml") -> BotConfig:
    """Loads configuration from YAML file and merges environment variables."""
    raw_dict = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            raw_dict = yaml.safe_load(f) or {}

    # Extract API key and environment overrides
    api_key = os.getenv("OPENSEA_API_KEY", raw_dict.get("opensea_api_key"))
    if os.getenv("BOT_TIMEZONE"):
        raw_dict.setdefault("general", {})["bot_timezone"] = os.getenv("BOT_TIMEZONE")
    if os.getenv("LOG_LEVEL"):
        raw_dict.setdefault("general", {})["log_level"] = os.getenv("LOG_LEVEL")

    raw_dict["opensea_api_key"] = api_key
    return BotConfig(**raw_dict)
