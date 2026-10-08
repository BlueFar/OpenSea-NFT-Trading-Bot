import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.config.settings import (
    BotConfig,
    GeneralConfig,
    DiscoveryConfig,
    FiltersConfig,
    FloorHistoryConfig,
    OfferToFloorFilterConfig,
    NetProfitFilterConfig,
)
from src.collectors.orchestrator import CollectionEvaluator
from src.discovery.engine import DiscoveryEngine
from src.filters.engine import FilterEngine
from src.models.collection import CollectionStats, Offer, CollectionMetadata
from src.models.filters import FilterResultStatus
from src.providers.opensea.parser import parse_listings, parse_offers
from src.storage.state_store import StateStore
from src.trade_model.calculator import compute_trade_economics
from src.utils.time import now_local
from tests.mock_data import make_sample_collection, make_sample_sales_events


def _today():
    return now_local("Asia/Kolkata").strftime("%Y-%m-%d")


def _passing_provider(floor=1.5, offer=0.9):
    provider = MagicMock()
    provider.get_collection.return_value = make_sample_collection(slug="snap-col", total_supply=10000)
    provider.get_active_listings_count.return_value = (300, False)
    provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=floor * 0.97, offer_price=floor * 0.5)
    provider.get_collection_stats.return_value = CollectionStats(floor_price=floor, floor_price_symbol="ETH")
    provider.get_top_offer.return_value = Offer(order_hash="0x1", chain="ethereum", price_value=offer, price_currency="WETH")
    return provider


def _config(temp_dir, **kwargs):
    return BotConfig(
        general=GeneralConfig(data_root=os.path.join(temp_dir, "data"), state_db_path=os.path.join(temp_dir, "bot.db")),
        floor_history=FloorHistoryConfig(use_provider_endpoint=False),
        **kwargs,
    )


# ------------------------------------------------------------------------------
# Bot-recorded floor history
# ------------------------------------------------------------------------------

def test_floor_checks_use_bot_snapshots():
    with tempfile.TemporaryDirectory() as temp_dir:
        cfg = _config(temp_dir)
        store = StateStore(cfg.general.state_db_path)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        store.record_floor_snapshot("snap-col", now_ts - 86400 + 1800, 1.48)       # ~24h ago
        store.record_floor_snapshot("snap-col", now_ts - 7 * 86400 - 3600, 1.45)   # ~7d ago

        provider = _passing_provider()
        report = CollectionEvaluator(provider, store, cfg).evaluate_collection("snap-col", dry_run=True)

        assert report is not None and report.is_overall_pass is True
        assert report.criteria["floor_change_1d"].actual_value == pytest.approx(1.35, abs=0.01)
        assert report.criteria["floor_change_7d"].actual_value == pytest.approx(3.45, abs=0.01)
        provider.get_floor_price_history.assert_not_called()
        # Current floor was stored for future evaluations
        assert store.get_floor_snapshot_near("snap-col", now_ts, 60) is not None


def test_missing_floor_history_is_recorded_as_warmup_rejection():
    with tempfile.TemporaryDirectory() as temp_dir:
        cfg = _config(temp_dir)
        store = StateStore(cfg.general.state_db_path)
        store.add_discovered_slugs(["snap-col"])

        report = CollectionEvaluator(_passing_provider(), store, cfg).evaluate_collection("snap-col", dry_run=True)

        assert report is None
        assert store.get_rejection_funnel(_today()) == {"floor_history_1d": 1}


def test_snapshot_outside_tolerance_is_ignored():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = StateStore(os.path.join(temp_dir, "bot.db"))
        now_ts = 1_800_000_000
        store.record_floor_snapshot("c", now_ts - 86400 - 7 * 3600, 1.0)  # 31h ago
        assert store.get_floor_snapshot_near("c", now_ts - 86400, 6 * 3600) is None
        store.record_floor_snapshot("c", now_ts - 86400 + 2 * 3600, 1.1)  # 22h ago
        assert store.get_floor_snapshot_near("c", now_ts - 86400, 6 * 3600)["floor_price"] == 1.1


# ------------------------------------------------------------------------------
# Rejection funnel & shortlist scheduling
# ------------------------------------------------------------------------------

def test_early_rejection_reason_is_recorded():
    with tempfile.TemporaryDirectory() as temp_dir:
        cfg = _config(temp_dir)
        store = StateStore(cfg.general.state_db_path)
        provider = _passing_provider()
        provider.get_collection.return_value = make_sample_collection(slug="snap-col", safelist_status="not_requested")

        assert CollectionEvaluator(provider, store, cfg).evaluate_collection("snap-col") is None
        assert store.get_rejection_funnel(_today()) == {"verification": 1}


def test_later_failure_does_not_overwrite_same_day_pass():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = StateStore(os.path.join(temp_dir, "bot.db"))
        store.record_candidate("c", "2026-10-03", is_pass=True, reasons="PASS")
        store.record_candidate("c", "2026-10-03", is_pass=False, reasons="listed_items: 7%", reject_filter="listed_items")
        assert store.is_candidate_recorded_today("c", "2026-10-03") is True
        assert store.get_rejection_funnel("2026-10-03") == {"PASS": 1}


def test_shortlisted_collections_get_refreshed_ahead_of_old_ones():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = StateStore(os.path.join(temp_dir, "bot.db"))
        store.add_discovered_slugs(["old-1", "old-2", "short-1"])
        for s in ("old-1", "old-2", "short-1"):
            store.mark_collection_evaluated(s)
        store.set_shortlisted("short-1", True)
        store.add_discovered_slugs(["new-1", "new-2", "new-3"])

        due = store.get_collections_due_for_evaluation(limit=4, shortlist_refresh_seconds=0)
        assert due[0] == "short-1"
        assert set(due[1:]) == {"new-1", "new-2", "new-3"}


# ------------------------------------------------------------------------------
# Profit gates
# ------------------------------------------------------------------------------

def _economics(floor, offer, royalty=5.0, mp_fee=0.5):
    col = CollectionMetadata(slug="c", name="c", fees=[])
    from src.models.collection import Fee
    col.fees = [Fee(fee=mp_fee, recipient="opensea", required=True), Fee(fee=royalty, recipient="0xcreator", required=True)]
    return compute_trade_economics(current_floor=floor, observed_top_offer=offer, collection=col)


def _eval_profit_criteria(econ, filters=None):
    engine = FilterEngine(filters or FiltersConfig())
    return engine._eval_offer_to_floor(econ, "now"), engine._eval_net_profit(econ, "now")


def test_floor_premium_includes_royalty():
    # entry = 1.0 * 1.01 = 1.01; exit = 1.5 * 0.95 = 1.425; royalty 5% of exit = 0.07125
    econ = _economics(floor=1.5, offer=1.0)
    assert econ.modelled.effective_entry_cost == pytest.approx(1.08125)
    assert econ.modelled.floor_premium_over_effective_offer_pct == pytest.approx((1.5 - 1.08125) / 1.08125 * 100)
    offer_crit, _ = _eval_profit_criteria(econ)
    assert offer_crit.result == FilterResultStatus.FAIL  # 38.7% < 40%

    offer_crit, _ = _eval_profit_criteria(_economics(floor=1.5, offer=0.9))
    assert offer_crit.result == FilterResultStatus.PASS  # ~53%


def test_net_profit_gate():
    _, roi_crit = _eval_profit_criteria(_economics(floor=1.5, offer=0.9))
    assert roi_crit.result == FilterResultStatus.PASS

    # Thin spread: offer close to floor -> ROI below 10%
    _, roi_crit = _eval_profit_criteria(_economics(floor=1.25, offer=1.1))
    assert roi_crit.result == FilterResultStatus.FAIL

    # Disabled -> observe only
    filters = FiltersConfig(net_profit=NetProfitFilterConfig(enabled=False))
    _, roi_crit = _eval_profit_criteria(_economics(floor=1.25, offer=1.1), filters)
    assert roi_crit.result == FilterResultStatus.OBSERVE


def test_net_profit_fails_closed_when_fee_unknown():
    col = CollectionMetadata(slug="c", name="c", fees=[])
    econ = compute_trade_economics(current_floor=2.0, observed_top_offer=1.0, collection=col)
    _, roi_crit = _eval_profit_criteria(econ)
    assert roi_crit.result == FilterResultStatus.DATA_INSUFFICIENT


def test_configured_fee_fallback_is_labelled():
    col = CollectionMetadata(slug="c", name="c", fees=[])
    econ = compute_trade_economics(current_floor=2.0, observed_top_offer=1.0, collection=col, fallback_marketplace_fee_pct=1.0)
    assert econ.modelled.is_complete_and_reliable is True
    assert econ.observed.marketplace_fee_source == "CONFIG"
    assert econ.observed.creator_royalty_pct == 0.0
    assert econ.modelled.estimated_marketplace_fee == pytest.approx(1.9 * 0.01)


def test_legacy_offer_formula_still_selectable():
    filters = FiltersConfig(offer_to_floor=OfferToFloorFilterConfig(formula="modelled_entry_offer_to_floor"))
    offer_crit, _ = _eval_profit_criteria(_economics(floor=1.5, offer=1.0), filters)
    assert offer_crit.actual_value == pytest.approx(67.33, abs=0.01)
    assert offer_crit.result == FilterResultStatus.PASS


# ------------------------------------------------------------------------------
# Parser: unique listings, per-unit collection offers
# ------------------------------------------------------------------------------

def _listing(order_hash, token_id):
    return {
        "order_hash": order_hash,
        "chain": "ethereum",
        "price": {"current": {"currency": "ETH", "decimals": 18, "value": "1000000000000000000"}},
        "protocol_data": {"parameters": {"offer": [{"itemType": 2, "token": "0xABC", "identifierOrCriteria": token_id, "startAmount": "1"}]}},
    }


def test_listings_carry_token_key_for_unique_counting():
    listings, _ = parse_listings({"listings": [_listing("0x1", "7"), _listing("0x2", "7"), _listing("0x3", "8")]})
    assert {lst.token_key for lst in listings} == {"0xabc:7", "0xabc:8"}


def test_collection_offer_price_is_per_nft():
    data = {"offers": [{
        "order_hash": "0xo",
        "chain": "ethereum",
        "price": {"currency": "WETH", "decimals": 18, "value": "3000000000000000000"},
        "protocol_data": {"parameters": {"consideration": [{"itemType": 4, "token": "0xabc", "startAmount": "3"}]}},
    }]}
    offers, _ = parse_offers(data)
    assert offers[0].price_value == pytest.approx(1.0)


# ------------------------------------------------------------------------------
# Discovery: per-chain, volume-ordered, depth-limited
# ------------------------------------------------------------------------------

def test_discovery_crawls_each_chain_ordered_by_volume():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = StateStore(os.path.join(temp_dir, "bot.db"))
        provider = MagicMock()
        provider.discover_collections.side_effect = lambda **kw: ([f"{kw['chain']}-a"], f"{kw['chain']}-next")
        cfg = DiscoveryConfig(chains=["ethereum", "base"], max_pages_per_cycle=1)

        slugs = DiscoveryEngine(provider, store, cfg).discover_next_batch()

        assert set(slugs) == {"ethereum-a", "base-a"}
        assert all(c.kwargs["order_by"] == "seven_day_volume" for c in provider.discover_collections.call_args_list)
        assert store.get_checkpoint("collections:ethereum") == "ethereum-next"
        assert store.get_checkpoint("collections:base") == "base-next"


def test_discovery_restarts_from_top_after_max_depth():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = StateStore(os.path.join(temp_dir, "bot.db"))
        provider = MagicMock()
        provider.discover_collections.side_effect = lambda **kw: (["x"], "next-cursor")
        cfg = DiscoveryConfig(chains=["ethereum"], max_pages_per_cycle=2, max_depth_pages=3)
        engine = DiscoveryEngine(provider, store, cfg)

        engine.discover_next_batch()  # pages 1-2
        assert store.get_checkpoint("collections:ethereum") == "next-cursor"
        engine.discover_next_batch()  # page 3 hits the depth limit
        assert store.get_checkpoint("collections:ethereum") is None
        assert provider.discover_collections.call_count == 3
