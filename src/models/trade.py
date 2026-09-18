from dataclasses import dataclass
from typing import Optional

@dataclass
class ObservedMarketData:
    observed_top_offer: Optional[float] = None
    top_offer_currency: str = "WETH"
    current_floor: Optional[float] = None
    floor_currency: str = "ETH"
    marketplace_fee_pct: Optional[float] = None  # OpenSea fee percentage from API
    creator_royalty_pct: Optional[float] = None  # Creator fee percentage from API
    fees_reliable: bool = False                  # True only if fees were reliably extracted from API

@dataclass
class TradeAssumptions:
    entry_offer_premium_pct: float = 1.0         # Placed 1.0% above observed top offer
    target_sale_discount_from_floor_pct: float = 5.0 # Placed 5.0% below floor
    gas_estimate_eth: float = 0.005              # Gas estimate in ETH

@dataclass
class ModelledResults:
    modelled_entry_offer: Optional[float] = None
    target_exit_price: Optional[float] = None
    gross_spread: Optional[float] = None
    estimated_marketplace_fee: Optional[float] = None
    estimated_creator_royalty: Optional[float] = None
    estimated_gas_cost: Optional[float] = None
    estimated_net_profit: Optional[float] = None
    estimated_roi_pct: Optional[float] = None
    estimated_profit_margin_pct: Optional[float] = None
    entry_offer_to_floor_ratio_pct: Optional[float] = None
    floor_spread_to_entry_offer_pct: Optional[float] = None
    is_complete_and_reliable: bool = False
    status_note: str = ""

@dataclass
class TradeEconomics:
    observed: ObservedMarketData
    assumptions: TradeAssumptions
    modelled: ModelledResults
    currency: str = "ETH"
    disclaimer: str = (
        "This is an observed market snapshot and theoretical trade model based on specified assumptions. "
        "It is not a guarantee of execution, liquidity, or profit."
    )
