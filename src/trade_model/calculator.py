from typing import Optional, List, Set, Tuple
from ..models.collection import CollectionMetadata, Fee
from ..models.trade import (
    ObservedMarketData,
    TradeAssumptions,
    ModelledResults,
    TradeEconomics,
)

KNOWN_OPENSEA_FEE_RECIPIENTS = {
    "0x0000a26b00c1f0df003000390027140000faa719",  # Seaport 1.5/1.6 OpenSea Fee Collector
    "0x5b3256965e7c3cf26e11fcaf296dfc8807c01073",  # OpenSea Legacy Fee Wallet
    # Chain-specific OpenSea fee recipients (OpenSea SDK 12.11.2, src/constants.ts)
    "0x07d3a100c3880830dd43fe5c938b5144721ce9d6",  # Alternate fee recipient (MegaETH)
    "0xdfe1593dca6ad8a20eeb418643e48577c1626f7c",  # Somnia
    "0xd9f68d28e451a83affdb7c71cc2c20552555b07f",  # Gunzilla (GUNZ)
}

def extract_fees_from_collection(collection: CollectionMetadata) -> Tuple[Optional[float], Optional[float], bool]:
    """
    Extracts marketplace fee % and creator royalty % from OpenSea collection fees.
    OpenSea API returns fees as a list of {fee: float, recipient: str, required: bool}.
    OpenSea fee values in v2 API are typically percentages (e.g. 1.0 for 1.0% or 0.01).
    If the marketplace fee cannot be determined reliably from collection metadata,
    returns (None, creator_royalty_pct, False).
    """
    if not collection.fees:
        return None, None, False

    marketplace_fee_pct: Optional[float] = None
    creator_royalty_pct: Optional[float] = None
    mp_fee_found = False

    for f in collection.fees:
        fee_val = f.fee
        # OpenSea v2 API returns fee as a float. In some endpoints 0.5 represents 0.5%, in others 0.005 represents 0.5%.
        # If fee_val < 0.1 (e.g. 0.025 or 0.005), it is expressed in decimal (2.5% or 0.5%).
        # If fee_val >= 0.1 (e.g. 2.5 or 0.5), it is expressed in percent.
        normalized_fee_pct = (fee_val * 100.0) if fee_val < 0.1 else fee_val

        recipient_lower = f.recipient.lower() if f.recipient else ""
        if "opensea" in recipient_lower or recipient_lower in KNOWN_OPENSEA_FEE_RECIPIENTS:
            marketplace_fee_pct = (marketplace_fee_pct or 0.0) + normalized_fee_pct
            mp_fee_found = True
        else:
            creator_royalty_pct = (creator_royalty_pct or 0.0) + normalized_fee_pct

    # If OpenSea marketplace fee is not reliably present in metadata, do NOT assume a fee
    if not mp_fee_found:
        return None, creator_royalty_pct, False

    return marketplace_fee_pct, (creator_royalty_pct or 0.0), True

def compute_trade_economics(
    current_floor: Optional[float],
    observed_top_offer: Optional[float],
    floor_currency: str = "ETH",
    top_offer_currency: str = "WETH",
    collection: Optional[CollectionMetadata] = None,
    entry_offer_premium_pct: float = 1.0,
    target_sale_discount_from_floor_pct: float = 5.0,
    gas_estimate_eth: float = 0.005,
    fallback_marketplace_fee_pct: Optional[float] = None,
    currency_groups: Optional[List[Set[str]]] = None,
) -> TradeEconomics:
    """
    Calculates theoretical trade economics according to user specifications:
    - Distinguishes observed market data, model assumptions, and modelled results.
    - Marketplace fee comes from collection metadata; if absent, the explicitly configured
      fallback_marketplace_fee_pct is used and labelled CONFIG. With no fallback, marks incomplete.
    - Handles currency parity (ETH == WETH, and per chain e.g. APE == WAPE via currency_groups).
    """
    # Extract fees from collection if available
    mp_fee_pct = None
    creator_fee_pct = None
    fees_reliable = False
    mp_fee_source = "UNKNOWN"
    if collection is not None:
        mp_fee_pct, creator_fee_pct, fees_reliable = extract_fees_from_collection(collection)
        if fees_reliable:
            mp_fee_source = "API"
    if not fees_reliable and fallback_marketplace_fee_pct is not None:
        mp_fee_pct = fallback_marketplace_fee_pct
        creator_fee_pct = creator_fee_pct or 0.0  # Empty fee schedule = no creator royalty configured
        fees_reliable = True
        mp_fee_source = "CONFIG"

    observed = ObservedMarketData(
        observed_top_offer=observed_top_offer,
        top_offer_currency=top_offer_currency,
        current_floor=current_floor,
        floor_currency=floor_currency,
        marketplace_fee_pct=mp_fee_pct,
        creator_royalty_pct=creator_fee_pct,
        fees_reliable=fees_reliable,
        marketplace_fee_source=mp_fee_source,
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

    # Check currency compatibility: a coin and its wrapped form are 1:1 interchangeable
    groups = currency_groups or [{"ETH", "WETH"}]
    fc, oc = (floor_currency or "").upper(), (top_offer_currency or "").upper()
    is_compatible_currency = fc == oc or any(fc in g and oc in g for g in groups)
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

    # "Floor above 40% of top offer, including royalty": the entry cost includes the creator
    # royalty owed when the NFT is resold at the target exit price.
    effective_entry_cost = modelled_entry_offer + target_exit_price * ((creator_fee_pct or 0.0) / 100.0)
    floor_premium_over_effective_offer = (
        ((current_floor - effective_entry_cost) / effective_entry_cost) * 100.0 if effective_entry_cost > 0 else None
    )

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
                effective_entry_cost=effective_entry_cost,
                floor_premium_over_effective_offer_pct=floor_premium_over_effective_offer,
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
            effective_entry_cost=effective_entry_cost,
            floor_premium_over_effective_offer_pct=floor_premium_over_effective_offer,
            is_complete_and_reliable=True,
            status_note="COMPLETE: Calculated using observed market values and stated model assumptions."
        ),
        currency=floor_currency,
    )
