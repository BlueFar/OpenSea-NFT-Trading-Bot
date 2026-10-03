from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

@dataclass
class Contract:
    address: str
    chain: str

@dataclass
class Fee:
    fee: float  # Percentage, e.g. 0.5 = 0.5%, or OpenSea's raw decimal representation (e.g. 0.005)
    recipient: str
    required: bool

@dataclass
class PaymentToken:
    symbol: str
    address: str
    chain: str
    decimals: int = 18
    eth_price: Optional[str] = None
    usd_price: Optional[str] = None

@dataclass
class CollectionMetadata:
    slug: str
    name: str
    description: Optional[str] = None
    created_date: Optional[str] = None  # OpenSea creation date: YYYY-MM-DD
    opensea_url: Optional[str] = None
    project_url: Optional[str] = None
    twitter_username: Optional[str] = None
    discord_url: Optional[str] = None
    instagram_username: Optional[str] = None
    telegram_url: Optional[str] = None
    wiki_url: Optional[str] = None
    safelist_status: Optional[str] = None  # e.g. "verified", "approved", "not_requested"
    is_disabled: bool = False
    is_nsfw: bool = False
    total_supply: Optional[int] = None
    contracts: List[Contract] = field(default_factory=list)
    fees: List[Fee] = field(default_factory=list)
    raw_data: Dict[str, Any] = field(default_factory=dict)

@dataclass
class CollectionStats:
    floor_price: Optional[float] = None
    floor_price_symbol: str = "ETH"
    num_owners: Optional[int] = None
    total_sales: Optional[int] = None
    one_day_sales: Optional[int] = None
    seven_day_sales: Optional[int] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)

@dataclass
class FloorPricePoint:
    time: int  # Unix timestamp (seconds)
    token_unit: float
    symbol: str = "ETH"
    chain: Optional[str] = None
    usd_price: Optional[str] = None

@dataclass
class SaleEvent:
    event_id: str  # Composite unique identifier
    chain: str
    event_timestamp: int  # Unix timestamp (seconds)
    transaction: Optional[str] = None
    order_hash: Optional[str] = None
    contract_address: Optional[str] = None
    token_id: Optional[str] = None
    quantity: int = 1
    seller: Optional[str] = None
    buyer: Optional[str] = None
    price_value: Optional[float] = None
    price_currency: Optional[str] = "ETH"

@dataclass
class Listing:
    order_hash: str
    chain: str
    price_value: float
    price_currency: str
    status: str = "ACTIVE"
    remaining_quantity: int = 1
    order_created_at: Optional[int] = None
    token_key: Optional[str] = None  # "<contract>:<token_id>" when available, to count unique listed NFTs

@dataclass
class Offer:
    order_hash: str
    chain: str
    price_value: float
    price_currency: str
    status: str = "ACTIVE"
    remaining_quantity: int = 1
    order_created_at: Optional[int] = None
