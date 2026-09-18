import pytest
from src.trade_model.calculator import compute_trade_economics
from src.models.collection import CollectionMetadata, Fee

def test_trade_model_complete_calculation():
    """Validates complete calculation when reliable fees are provided."""
    col = CollectionMetadata(
        slug="test-col",
        name="Test Col",
        fees=[
            Fee(fee=0.005, recipient="opensea", required=True),    # 0.5% marketplace fee
            Fee(fee=0.05, recipient="0xcreator", required=True),   # 5.0% creator royalty
        ]
    )

    current_floor = 2.0     # 2.0 ETH
    observed_offer = 1.0    # 1.0 WETH

    econ = compute_trade_economics(
        current_floor=current_floor,
        observed_top_offer=observed_offer,
        floor_currency="ETH",
        top_offer_currency="WETH",
        collection=col,
        entry_offer_premium_pct=1.0,               # +1% premium
        target_sale_discount_from_floor_pct=5.0,   # -5% discount from floor
        gas_estimate_eth=0.01,
    )

    assert econ.modelled.is_complete_and_reliable is True
    # 1. Entry offer: 1.0 * (1 + 0.01) = 1.01 ETH
    assert pytest.approx(econ.modelled.modelled_entry_offer, rel=1e-4) == 1.01
    # 2. Target exit: 2.0 * (1 - 0.05) = 1.90 ETH
    assert pytest.approx(econ.modelled.target_exit_price, rel=1e-4) == 1.90
    # 3. Gross spread: 1.90 - 1.01 = 0.89 ETH
    assert pytest.approx(econ.modelled.gross_spread, rel=1e-4) == 0.89
    # 4. Marketplace fee: 1.90 * 0.005 = 0.0095 ETH
    assert pytest.approx(econ.modelled.estimated_marketplace_fee, rel=1e-4) == 0.0095
    # 5. Creator royalty: 1.90 * 0.05 = 0.0950 ETH
    assert pytest.approx(econ.modelled.estimated_creator_royalty, rel=1e-4) == 0.0950
    # 6. Total costs: 0.0095 + 0.0950 + 0.01 (gas) = 0.1145 ETH
    # 7. Net profit: 0.89 - 0.1145 = 0.7755 ETH
    assert pytest.approx(econ.modelled.estimated_net_profit, rel=1e-4) == 0.7755
    # 8. ROI: (0.7755 / 1.01) * 100 = 76.78%
    assert pytest.approx(econ.modelled.estimated_roi_pct, rel=1e-3) == 76.782

def test_trade_model_unreliable_fees_marks_incomplete():
    """Validates that when marketplace fee cannot be determined, it does NOT assume 0.5% fallback."""
    col = CollectionMetadata(
        slug="no-fee-col",
        name="No Fee Col",
        fees=[]  # Empty fee schedule
    )

    econ = compute_trade_economics(
        current_floor=2.0,
        observed_top_offer=1.0,
        collection=col,
    )

    # Must be marked as incomplete / not reliable
    assert econ.modelled.is_complete_and_reliable is False
    assert econ.modelled.estimated_marketplace_fee is None
    assert econ.modelled.estimated_net_profit is None
    assert "INCOMPLETE" in econ.modelled.status_note

def test_trade_model_currency_mismatch():
    """Validates currency mismatch detection (e.g. USDC floor vs WETH offer)."""
    col = CollectionMetadata(slug="c", name="c", fees=[Fee(fee=0.005, recipient="opensea", required=True)])
    econ = compute_trade_economics(
        current_floor=1000.0,
        observed_top_offer=0.5,
        floor_currency="USDC",
        top_offer_currency="WETH",
        collection=col,
    )
    assert econ.modelled.is_complete_and_reliable is False
    assert "CURRENCY_MISMATCH" in econ.modelled.status_note

def test_trade_model_only_creator_royalty_marks_incomplete():
    """Validates that if only creator fee is present and no OpenSea fee, it does NOT assume 0% or fallback fee."""
    col = CollectionMetadata(
        slug="creator-only",
        name="Creator Only",
        fees=[Fee(fee=5.0, recipient="0xcreatoraddress", required=False)]
    )
    econ = compute_trade_economics(
        current_floor=1.0,
        observed_top_offer=0.5,
        collection=col,
    )
    assert econ.observed.marketplace_fee_pct is None
    assert econ.observed.fees_reliable is False
    assert econ.modelled.is_complete_and_reliable is False
    assert econ.modelled.estimated_marketplace_fee is None
    assert "INCOMPLETE" in econ.modelled.status_note

def test_trade_model_known_seaport_fee_recipient():
    """Validates OpenSea official Seaport fee collector address extraction."""
    col = CollectionMetadata(
        slug="doodles",
        name="Doodles",
        fees=[
            Fee(fee=1.0, recipient="0x0000a26b00c1f0df003000390027140000faa719", required=True),
            Fee(fee=5.0, recipient="0xd1f124cc900624e1ff2d923180b3924147364380", required=False),
        ]
    )
    econ = compute_trade_economics(
        current_floor=2.0,
        observed_top_offer=1.0,
        collection=col,
        entry_offer_premium_pct=1.0,
        target_sale_discount_from_floor_pct=5.0,
        gas_estimate_eth=0.005,
    )
    assert econ.observed.marketplace_fee_pct == 1.0
    assert econ.observed.creator_royalty_pct == 5.0
    assert econ.observed.fees_reliable is True
    assert econ.modelled.is_complete_and_reliable is True
    # Exit price = 2.0 * 0.95 = 1.90
    # MP fee = 1.90 * 0.01 = 0.019 ETH
    assert pytest.approx(econ.modelled.estimated_marketplace_fee, rel=1e-4) == 0.019
    # Creator fee = 1.90 * 0.05 = 0.095 ETH
    assert pytest.approx(econ.modelled.estimated_creator_royalty, rel=1e-4) == 0.095

