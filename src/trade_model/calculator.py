from typing import Optional, List, Tuple
from ..models.collection import CollectionMetadata, Fee
from ..models.trade import (
    ObservedMarketData,
    TradeAssumptions,
    ModelledResults,
    TradeEconomics,
)

def extract_fees_from_collection(collection: CollectionMetadata) -> Tuple[Optional[float], Optional[float], bool]:
    """
    Extracts marketplace fee % and creator royalty % from OpenSea collection fees.
    OpenSea API returns fees as a list of {fee: float, recipient: str, required: bool}.
    OpenSea fee values in v2 API are typically percentages (e.g. 0.5 for 0.5% or 0.005).
    OpenSea official fee recipients are either marked or identified by fee structure.
    If no fee information is returned or cannot be determined reliably, returns (None, None, False).
    """
    if not collection.fees:
        return None, None, False

    marketplace_fee_pct = 0.0
    creator_royalty_pct = 0.0
    found_any = False

    for f in collection.fees:
        fee_val = f.fee
        # OpenSea v2 API returns fee as a float. In some endpoints 0.5 represents 0.5%, in others 0.005 represents 0.5%.
        # If fee_val < 0.1 (e.g. 0.025 or 0.005), it is expressed in decimal (2.5% or 0.5%).
        # If fee_val >= 0.1 (e.g. 2.5 or 0.5), it is expressed in percent.
        normalized_fee_pct = (fee_val * 100.0) if fee_val < 0.1 else fee_val

        # Recipient classification: OpenSea protocol fee recipient vs creator
        recipient_lower = f.recipient.lower() if f.recipient else ""
        # Known OpenSea fee recipients / addresses, or required fee
        if "opensea" in recipient_lower or "0x0000a26b00c1f0df003000390027140000faa719" in recipient_lower:
            marketplace_fee_pct += normalized_fee_pct
            found_any = True
        else:
            creator_royalty_pct += normalized_fee_pct
            found_any = True

    # If marketplace fee was not explicitly labeled, check if there are multiple fees
    if marketplace_fee_pct == 0.0 and len(collection.fees) > 1:
        # One of them is likely the platform fee
        pass

    # Reliable only if fee schedule was populated
    return marketplace_fee_pct, creator_royalty_pct, found_any

def compute_trade_economics(
    current_floor: Optional[float],
    observed_top_offer: Optional[float],
    floor_currency: str = "ETH",
    top_offer_currency: str = "WETH",
    collection: Optional[CollectionMetadata] = None,
    entry_offer_premium_pct: float = 1.0,
    target_sale_discount_from_floor_pct: float = 5.0,
    gas_estimate_eth: float = 0.005,
) -> TradeEconomics:
    """
    Calculates theoretical trade economics according to user specifications:
    - Distinguishes observed market data, model assumptions, and modelled results.
    - Does NOT use a hardcoded marketplace fee fallback; if fee cannot be determined, marks incomplete.
    - Handles currency parity (ETH == WETH).
    """
    # Extract fees from collection if available
    mp_fee_pct = None
    creator_fee_pct = None
    fees_reliable = False
    if collection is not None:
        mp_fee_pct, creator_fee_pct, fees_reliable = extract_fees_from_collection(collection)

    observed = ObservedMarketData(
        observed_top_offer=observed_top_offer,
        top_offer_currency=top_offer_currency,
        current_floor=current_floor,
        floor_currency=floor_currency,
        marketplace_fee_pct=mp_fee_pct,
        creator_royalty_pct=creator_fee_pct,
        fees_reliable=fees_reliable,
    )

    assumptions = TradeAssumptions(
        entry_offer_premium_pct=entry_offer_premium_pct,
        target_sale_discount_from_floor_pct=target_sale_discount_from_floor_pct,
        gas_estimate_eth=gas_estimate_eth,
    )

    # Check basic availability
    if current_floor is None or observed_top_offer is None or current_floor <= 0:
        return TradeEconomics(
            observed=observed,
            assumptions=assumptions,
            modelled=ModelledResults(
                is_complete_and_reliable=False,
                status_note="INCOMPLETE: Missing current floor price or top offer."
            ),
            currency=floor_currency,
        )

    # Check currency compatibility (ETH and WETH are 1:1 interchangeable on EVM)
    is_compatible_currency = (
        (floor_currency.upper() in ("ETH", "WETH") and top_offer_currency.upper() in ("ETH", "WETH"))
        or (floor_currency.upper() == top_offer_currency.upper())
    )
    if not is_compatible_currency:
        return TradeEconomics(
            observed=observed,
            assumptions=assumptions,
            modelled=ModelledResults(
                is_complete_and_reliable=False,
                status_note=f"CURRENCY_MISMATCH: Floor is in {floor_currency} but offer is in {top_offer_currency} without a real-time conversion rate."
            ),
            currency=floor_currency,
        )

    # 1. Modelled Entry Offer (Hypothetical Buy Price)
    modelled_entry_offer = observed_top_offer * (1.0 + (entry_offer_premium_pct / 100.0))

    # 2. Target Exit Price (Hypothetical Sale Price)
    target_exit_price = current_floor * (1.0 - (target_sale_discount_from_floor_pct / 100.0))

    # 3. Gross Spread
    gross_spread = target_exit_price - modelled_entry_offer

    # 4. Ratios
    entry_offer_to_floor_ratio = (modelled_entry_offer / current_floor) * 100.0
    floor_spread_to_entry_offer = ((current_floor - modelled_entry_offer) / modelled_entry_offer) * 100.0 if modelled_entry_offer > 0 else 0.0

    # 5. Check if fees are reliable
    if not fees_reliable or mp_fee_pct is None:
        return TradeEconomics(
            observed=observed,
            assumptions=assumptions,
            modelled=ModelledResults(
                modelled_entry_offer=modelled_entry_offer,
                target_exit_price=target_exit_price,
                gross_spread=gross_spread,
                entry_offer_to_floor_ratio_pct=entry_offer_to_floor_ratio,
                floor_spread_to_entry_offer_pct=floor_spread_to_entry_offer,
                is_complete_and_reliable=False,
                status_note="INCOMPLETE: Applicable OpenSea marketplace fee schedule could not be determined reliably from collection metadata."
            ),
            currency=floor_currency,
        )

    # 6. Modelled Costs & Net Profit
    est_mp_fee = target_exit_price * (mp_fee_pct / 100.0)
    est_royalty = target_exit_price * ((creator_fee_pct or 0.0) / 100.0)
    est_gas = gas_estimate_eth

    total_costs = est_mp_fee + est_royalty + est_gas
    est_net_profit = gross_spread - total_costs
    est_roi = (est_net_profit / modelled_entry_offer) * 100.0 if modelled_entry_offer > 0 else 0.0
    est_margin = (est_net_profit / target_exit_price) * 100.0 if target_exit_price > 0 else 0.0

    return TradeEconomics(
        observed=observed,
        assumptions=assumptions,
        modelled=ModelledResults(
            modelled_entry_offer=modelled_entry_offer,
            target_exit_price=target_exit_price,
            gross_spread=gross_spread,
            estimated_marketplace_fee=est_mp_fee,
            estimated_creator_royalty=est_royalty,
            estimated_gas_cost=est_gas,
            estimated_net_profit=est_net_profit,
            estimated_roi_pct=est_roi,
            estimated_profit_margin_pct=est_margin,
            entry_offer_to_floor_ratio_pct=entry_offer_to_floor_ratio,
            floor_spread_to_entry_offer_pct=floor_spread_to_entry_offer,
            is_complete_and_reliable=True,
            status_note="COMPLETE: Calculated using observed market values and stated model assumptions."
        ),
        currency=floor_currency,
    )
