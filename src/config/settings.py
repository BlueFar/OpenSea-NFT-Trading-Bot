import os
from typing import Any, Dict, List, Optional
import yaml
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from .chains import ALL_CHAIN_IDS

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")


def reload_env() -> Optional[str]:
    """
    Reads .env again, replacing values already in the environment, and returns the OpenSea key.
    override=True matters: the bot is started by the dashboard and would otherwise inherit the
    key the dashboard read when it started, ignoring a key changed in .env since then.
    """
    if os.path.exists(ENV_PATH):
        load_dotenv(ENV_PATH, override=True)
    else:
        load_dotenv(override=True)
    return os.getenv("OPENSEA_API_KEY")


# Load .env if present
reload_env()

class GeneralConfig(BaseModel):
    bot_timezone: str = "Asia/Kolkata"
    data_root: str = "./data"
    state_db_path: str = "./state/bot.db"
    log_level: str = "INFO"

class DiscoveryConfig(BaseModel):
    primary_source: str = "collections"
    batch_size: int = 50
    max_pages_per_cycle: int = 2
    order_by: Optional[str] = "seven_day_volume"  # /api/v2/collections order_by; None = unordered catalog
    max_depth_pages: int = 20  # Restart each chain's crawl from the top after this many pages (0 = unlimited)
    enable_top: bool = False
    enable_trending: bool = False
    chains: List[str] = Field(default_factory=lambda: list(ALL_CHAIN_IDS))
    # Skip collections whose discovery listing already shows no blue tick (saves API calls)
    skip_unverified: bool = True
    watchlist: List[str] = Field(default_factory=list)

class TradingFrequencyFilterConfig(BaseModel):
    enabled: bool = True
    metric: str = "average_daily_sales"  # Options: average_daily_sales, max_daily_sales, both
    max_threshold: float = 2.0
    min_sales_7d: int = 1  # At least this many sales in the last 7 days (0 = off)
    # At least this many of them bought at about the floor price, not by accepting the top offer (0 = off)
    min_floor_sales_7d: int = 1
    floor_sale_min_pct: float = 90.0   # a floor-price sale paid at least this % of the floor at the time...
    floor_sale_max_pct: float = 115.0  # ...and at most this % (above it is usually a rare item)
    # A floor-price sale of an item in the rarest this % of the collection doesn't count (0 = off)
    rare_item_pct: float = 10.0
    # At least this many sales where a seller accepted a collection offer in the last 14 days (0 = off)
    min_offer_sales_14d: int = 1
    sale_count_mode: str = "transactions"  # Options: transactions, item_quantity

class FloorChangeFilterConfig(BaseModel):
    enabled: bool = True
    max_change_pct: float

class OfferToFloorFilterConfig(BaseModel):
    enabled: bool = True
    min_ratio_pct: float = 40.0
    # Options: floor_premium_over_effective_offer, floor_spread_to_entry_offer, modelled_entry_offer_to_floor
    formula: str = "floor_premium_over_effective_offer"

class NetProfitFilterConfig(BaseModel):
    enabled: bool = True
    min_net_roi_pct: float = 10.0

class ListedItemsFilterConfig(BaseModel):
    enabled: bool = True
    max_listed_pct: float = 6.0

class VerificationFilterConfig(BaseModel):
    enabled: bool = True
    required_status: List[str] = Field(default_factory=lambda: ["verified"])

class ProjectAgeFilterConfig(BaseModel):
    enabled: bool = True
    min_age_days: float = 60.0

class FiltersConfig(BaseModel):
    trading_frequency: TradingFrequencyFilterConfig = Field(default_factory=TradingFrequencyFilterConfig)
    floor_change_1d: FloorChangeFilterConfig = Field(default_factory=lambda: FloorChangeFilterConfig(max_change_pct=8.0))
    floor_change_7d: FloorChangeFilterConfig = Field(default_factory=lambda: FloorChangeFilterConfig(max_change_pct=10.0))
    offer_to_floor: OfferToFloorFilterConfig = Field(default_factory=OfferToFloorFilterConfig)
    net_profit: NetProfitFilterConfig = Field(default_factory=NetProfitFilterConfig)
    listed_items: ListedItemsFilterConfig = Field(default_factory=ListedItemsFilterConfig)
    verification: VerificationFilterConfig = Field(default_factory=VerificationFilterConfig)
    project_age: ProjectAgeFilterConfig = Field(default_factory=ProjectAgeFilterConfig)

class TradeModelConfig(BaseModel):
    entry_offer_premium_pct: float = 1.0
    target_sale_discount_from_floor_pct: float = 5.0
    gas_estimate_eth: float = 0.005
    # Used only when the OpenSea fee is not present in collection metadata. None = mark economics INCOMPLETE.
    marketplace_fee_pct: Optional[float] = 1.0
    # Per-chain gas per trade in the chain's own coin; chains not listed use src/config/chains.py defaults
    chain_gas: Dict[str, float] = Field(default_factory=dict)

class FloorHistoryConfig(BaseModel):
    snapshot_retention_days: int = 10
    one_day_tolerance_hours: float = 6.0      # Accept a bot snapshot taken 24h +/- this
    seven_day_tolerance_hours: float = 24.0   # Accept a bot snapshot taken 7d +/- this
    use_provider_endpoint: bool = True        # Fall back to /collections/{slug}/floor_prices when no snapshot
    # After downtime (e.g. a power cut) there may be no snapshot inside the normal window.
    # Then the nearest snapshot within this wider window is used and labelled in Info.md. 0 = off.
    gap_fallback_1d_hours: float = 12.0
    gap_fallback_7d_hours: float = 48.0

class SchedulerConfig(BaseModel):
    discovery_interval_seconds: int = 3600
    candidate_refresh_interval_seconds: int = 300
    request_delay_seconds: float = 0.6  # 100 requests a minute at most, under the key's 120
    rate_limit_backoff_factor: float = 2.0
    max_retries: int = 5
    evaluations_per_cycle: int = 40
    shortlist_refresh_seconds: int = 10800  # Re-check collections that passed the cheap filters this often
    chain_volume_interval_seconds: int = 21600  # Refresh the "Most active chains" table this often (0 = off)

class RuntimeConfig(BaseModel):
    wait_for_internet: bool = True            # Pause while offline instead of skipping collections
    connectivity_host: str = "api.opensea.io"
    offline_check_seconds: int = 30
    notify_new_candidates: bool = True        # macOS notification banner for each new candidate
    notification_sound: bool = False
    ipv4_only: bool = True                    # Connect to OpenSea over IPv4 only (broken home IPv6 stalls every call)

class BotConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    trade_model: TradeModelConfig = Field(default_factory=TradeModelConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    floor_history: FloorHistoryConfig = Field(default_factory=FloorHistoryConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    opensea_api_key: Optional[str] = None

def overrides_path_for(config_path: str) -> str:
    """Settings saved from the dashboard live next to config.yaml so its comments stay intact."""
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), "overrides.yaml")


def deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(config_path: str = "config/config.yaml", use_overrides: bool = True) -> BotConfig:
    """Loads configuration from YAML file, dashboard overrides and environment variables."""
    raw_dict = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            raw_dict = yaml.safe_load(f) or {}

    overrides_path = overrides_path_for(config_path)
    if use_overrides and os.path.exists(overrides_path):
        with open(overrides_path, "r", encoding="utf-8") as f:
            raw_dict = deep_merge(raw_dict, yaml.safe_load(f) or {})

    # Extract API key and environment overrides (.env is re-read so a changed key is used)
    reload_env()
    api_key = os.getenv("OPENSEA_API_KEY", raw_dict.get("opensea_api_key"))
    if os.getenv("BOT_TIMEZONE"):
        raw_dict.setdefault("general", {})["bot_timezone"] = os.getenv("BOT_TIMEZONE")
    if os.getenv("LOG_LEVEL"):
        raw_dict.setdefault("general", {})["log_level"] = os.getenv("LOG_LEVEL")

    raw_dict["opensea_api_key"] = api_key
    return BotConfig(**raw_dict)
