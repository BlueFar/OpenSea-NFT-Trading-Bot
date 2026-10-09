import os
"""How each sale happened (listing bought or offer accepted), rare items at floor price, offer sales in 14 days,
and the candidate list showing each collection's latest check."""
import tempfile
from unittest.mock import patch

import yaml

from src.config.chains import currency_groups
from src.config.settings import load_config
from src.metrics.floor_sales import (
    classify_floor_sales, classify_offer_sales, too_few_floor_sales_reason, too_few_offer_sales_reason,
)
from src.models.collection import SaleEvent
from src.providers.opensea.parser import parse_nft_rarity_rank, parse_order_info, parse_sale_events
from src.storage.state_store import StateStore
from tests.mock_data import make_sample_sales_events
from tests.test_mac_runtime_and_chains import _call, _evaluator, _settings_env

ETH = currency_groups("ethereum")
START, END = 1_000_000, 1_000_000 + 14 * 86400


def sale(i, price, ts=None, standard="erc721"):
    return SaleEvent(event_id=f"e{i}", chain="ethereum", event_timestamp=ts or START + 3600 * (i + 1),
                     transaction=f"tx{i}", order_hash=f"0xo{i}", protocol_address="0xseaport",
                     contract_address="0xC", token_id=str(i), price_value=price, price_currency="ETH",
                     seller=f"0xs{i}", buyer=f"0xb{i}", token_standard=standard)


# ---------------------------------------------------------------------------
# Parsing OpenSea's answers (shapes copied from real responses)
# ---------------------------------------------------------------------------
def _order(offer_type, criteria="missing", consideration_nft=None):
    order = {"protocol_data": {"parameters": {
        "offerer": "0xo",
        "offer": [{"itemType": offer_type, "token": "0xT", "identifierOrCriteria": "0"}],
        "consideration": [consideration_nft] if consideration_nft else [{"itemType": 0, "identifierOrCriteria": "0"}],
    }}}
    if criteria != "missing":
        order["criteria"] = criteria
    return {"order": order}


def test_order_lookup_tells_listings_and_each_kind_of_offer_apart():
    assert parse_order_info(_order(2)) == {"kind": "listing", "offer_type": None}
    collection = _order(1, {"collection": {"slug": "x"}, "traits": None, "numeric_traits": None, "encoded_token_ids": "*"},
                        {"itemType": 4, "identifierOrCriteria": "0"})
    assert parse_order_info(collection) == {"kind": "offer", "offer_type": "collection"}
    trait = _order(1, {"collection": {"slug": "x"}, "traits": [{"type": "Mouth", "value": "Smirk"}],
                       "encoded_token_ids": "0:11,13:19"}, {"itemType": 4, "identifierOrCriteria": "1033095"})
    assert parse_order_info(trait) == {"kind": "offer", "offer_type": "trait"}
    item = _order(1, None, {"itemType": 3, "identifierOrCriteria": "4"})
    assert parse_order_info(item) == {"kind": "offer", "offer_type": "item"}
    # No criteria key at all: the consideration item decides
    assert parse_order_info(_order(1, "missing", {"itemType": 4, "identifierOrCriteria": "0"}))["offer_type"] == "collection"
    assert parse_order_info(None) is None and parse_order_info({"errors": ["Order not found"]}) is None


def test_rarity_rank_and_sale_event_fields():
    assert parse_nft_rarity_rank({"nft": {"rarity": {"strategy_id": "openrarity", "rank": 1386}}}) == 1386
    assert parse_nft_rarity_rank({"nft": {"rarity": None}}) is None
    assert parse_nft_rarity_rank({"nft": {"rarity": {"rank": 0}}}) is None
    ev = parse_sale_events({"asset_events": [{
        "event_type": "sale", "event_timestamp": 1790885438, "transaction": "0xt", "order_hash": "0xh",
        "protocol_address": "0x0000000000000068f116a894984e2db1123eb395", "chain": "ape_chain",
        "payment": {"quantity": "1160000000000000000", "decimals": 18, "symbol": "WAPE"},
        "seller": "0xs", "buyer": "0xb", "quantity": 1,
        "nft": {"identifier": "1861", "contract": "0x4c6f", "token_standard": "erc721"}}]})[0]
    assert (ev.protocol_address, ev.token_standard, ev.chain, ev.token_id) == (
        "0x0000000000000068f116a894984e2db1123eb395", "erc721", "ape_chain", "1861")


# ---------------------------------------------------------------------------
# Floor-price sales: accepted offers and rare items don't count
# ---------------------------------------------------------------------------
def test_floor_priced_offer_or_rare_item_does_not_count():
    events = [sale(0, 1.0), sale(1, 0.98), sale(2, 0.99), sale(3, 1.01)]
    kinds = {"0xo0": "offer", "0xo1": "listing", "0xo2": "listing"}   # 0xo3: OpenSea didn't say
    ranks = {"1": 5, "2": 800}                                       # supply 1000: rank 5 is in the rarest 10%
    m = classify_floor_sales(events, START, END, 1.0, "ETH", ETH,
                             order_info=lambda e: {"kind": kinds[e.order_hash]} if e.order_hash in kinds else None,
                             rarity_rank=lambda e: ranks.get(e.token_id), supply=1000, rare_pct=10.0)
    labels = {r.price: (r.label, r.how, r.rank) for r in m.rows}
    assert labels[1.0] == ("offer", "offer", None)
    assert labels[0.98] == ("rare", "listing", 5)
    assert labels[0.99] == ("floor", "listing", 800)
    assert labels[1.01] == ("floor", "", None)        # unknown answers leave the sale counted
    assert (m.floor_sales, m.offers, m.rare) == (2, 1, 1)
    assert "1 accepted offer near the floor" in m.breakdown() and "1 rare item (rarest 10%)" in m.breakdown()
    assert m.summary(1)["rows"][0]["how"] in ("listing", "offer", "")


def test_reasons_when_only_rare_items_or_offers_were_near_the_floor():
    rare = classify_floor_sales([sale(0, 1.0)], START, END, 1.0, "ETH", ETH,
                                rarity_rank=lambda e: 1, supply=100, rare_pct=10.0)
    assert too_few_floor_sales_reason(rare, 1) == "The only sales at floor price were rare items"
    offer = classify_floor_sales([sale(0, 1.0)], START, END, 1.0, "ETH", ETH, order_info=lambda e: {"kind": "offer"})
    assert too_few_floor_sales_reason(offer, 1) == "Sales near the floor were accepted offers, not listings bought"
    off = classify_floor_sales([sale(0, 1.0)], START, END, 1.0, "ETH", ETH,
                               rarity_rank=lambda e: 1, supply=100, rare_pct=0)
    assert off.floor_sales == 1 and off.rare_pct == 0


# ---------------------------------------------------------------------------
# Offer sales in 14 days
# ---------------------------------------------------------------------------
def offers(events, info=None, floor=1.0):
    return classify_offer_sales(events, START, END, floor, "ETH", ETH, order_info=info)


def test_collection_offers_count_trait_and_item_offers_do_not():
    events = [sale(0, 0.6), sale(1, 0.62), sale(2, 0.7), sale(3, 0.98), sale(4, 1.6)]
    info = {"0xo0": {"kind": "offer", "offer_type": "collection"},
            "0xo1": {"kind": "offer", "offer_type": "trait"},
            "0xo2": {"kind": "offer", "offer_type": "item"},
            "0xo3": {"kind": "offer", "offer_type": "collection"},   # accepted near the floor: still an offer sale
            "0xo4": {"kind": "offer", "offer_type": "collection"}}   # above the band: never looked at
    seen = []
    m = offers(events, lambda e: seen.append(e.order_hash) or info[e.order_hash])
    assert (m.offer_sales, m.confirmed, m.by_price, m.other_offers, m.total) == (2, 2, 0, 2, 5)
    assert "0xo4" not in seen
    assert too_few_offer_sales_reason(m, 3) == "Only 2 sales to a collection offer in 14 days (needs 3)"


def test_without_order_data_a_sale_below_the_floor_counts():
    m = offers([sale(0, 0.6), sale(1, 0.98)])
    assert (m.offer_sales, m.by_price) == (1, 1)
    listing = offers([sale(0, 0.6)], lambda e: {"kind": "listing"})
    assert listing.offer_sales == 0
    assert too_few_offer_sales_reason(listing, 1) == "No sales to a collection offer in the last 14 days"
    trait_only = offers([sale(0, 0.6)], lambda e: {"kind": "offer", "offer_type": "trait"})
    assert too_few_offer_sales_reason(trait_only, 1).startswith("No collection offers accepted in 14 days")
    assert offers([sale(0, 0.6)], floor=None) is None


# ---------------------------------------------------------------------------
# In a real check (floor 1.25 ETH, top offer 0.8 WETH)
# ---------------------------------------------------------------------------
def test_no_offer_sales_rejects_and_downloads_the_week_before_once():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        store.add_discovered_slugs(["solitary-voyagers"])
        week = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2)   # all floor-price listings
        calls = []

        def sales(slug, after_timestamp, before_timestamp=None, **kw):
            calls.append(before_timestamp)
            return [] if before_timestamp else week
        ev.provider.get_sale_events.side_effect = sales
        assert ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True) is None
        assert len(calls) == 2 and calls[0] is None and calls[1] is not None
        det = store.get_results_since("2000-01-01")[0]["details"]
        assert det["reason"] == "No sales to a collection offer in the last 14 days"
        assert (det["limit"], det["kind"], det["unit"]) == (1, "min", "offer sales in 14 days")
        assert det["offer_sales"]["count"] == 0 and "trading_frequency" not in det["checked"]
        assert store.count_shortlisted() == 1


def test_an_offer_sale_last_week_or_the_week_before_passes():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        week = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2, offer_price=0.7)
        ev.provider.get_sale_events.side_effect = lambda slug, after_timestamp, before_timestamp=None, **kw: (
            [] if before_timestamp else week)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.is_overall_pass
        assert ev.last_details["offer_sales"]["count"] == 1
        # The week before is still downloaded, so the dashboard lists all 14 days
        assert ev.provider.get_sale_events.call_count == 2
        assert ev.last_details["offer_sales"]["covered_days"] == 14

    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        week = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2)
        first = week[0]
        older = [SaleEvent(event_id="old", chain="ethereum", event_timestamp=first.event_timestamp - 6 * 86400,
                           transaction="0xold", order_hash="0xold", price_value=0.7, price_currency="WETH",
                           seller="0xq", buyer="0xr")]
        ev.provider.get_sale_events.side_effect = lambda slug, after_timestamp, before_timestamp=None, **kw: (
            older if before_timestamp else week)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.is_overall_pass
        assert ev.last_details["offer_sales"]["count"] == 1


def test_lookups_are_saved_and_capped_and_erc1155_skips_rarity():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        week = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2, offer_price=0.7)
        for e in week:
            e.protocol_address = "0xseaport"
        ev.provider.get_sale_events.return_value = week
        ev.provider.get_order_info.side_effect = lambda chain, proto, h: (
            {"kind": "offer", "offer_type": "collection"} if h == week[0].order_hash else {"kind": "listing", "offer_type": None})
        ev.provider.get_nft_rarity.return_value = (True, 9000)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.is_overall_pass
        first_orders = ev.provider.get_order_info.call_count
        assert 0 < first_orders <= 20 and ev.provider.get_nft_rarity.call_count <= 10
        assert store.get_sale_orders([week[0].order_hash])[week[0].order_hash]["kind"] == "offer"
        ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert ev.provider.get_order_info.call_count == first_orders   # second check uses the saved answers

    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        week = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2, offer_price=0.7)
        for e in week:
            e.token_standard = "erc1155"
        ev.provider.get_sale_events.return_value = week
        ev.provider.get_nft_rarity.return_value = (True, 1)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.is_overall_pass
        assert ev.provider.get_nft_rarity.call_count == 0


def test_rare_floor_sale_rejects_like_the_owner_saw():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        week = make_sample_sales_events([0, 0, 0, 0, 0, 0, 2], price=0.6)   # two accepted offers...
        week[-1].price_value = 1.2                                          # ...and one floor-price buy
        ev.provider.get_sale_events.return_value = week
        ev.provider.get_nft_rarity.return_value = (True, 3)                 # of a top-3 item
        assert ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True) is None
        det = store.get_results_since("2000-01-01")[0]["details"]
        assert det["reason"] == "The only sales at floor price were rare items"
        assert det["floor_sales"]["rare"] == 1


def test_offer_minimum_zero_or_rule_off_never_rejects():
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d, trading_frequency={"min_offer_sales_14d": 0})
        ev.provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.is_overall_pass
        assert ev.provider.get_sale_events.call_count == 1 and "offer_sales" not in ev.last_details
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d, trading_frequency={"enabled": False})
        ev.provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.criteria["trading_frequency"].result.value == "OBSERVE"


def test_inspect_shows_offer_sales_as_the_failing_part():
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d)
        ev.provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=False)
        assert report is not None and not report.is_overall_pass
        tf = ev.last_details["rules"]["trading_frequency"]
        assert (tf["limit"], tf["kind"], tf["unit"], tf["value"]) == (1, "min", "offer sales in 14 days", 0)
        assert tf["note"].startswith("No sales to a collection offer in the last 14 days")


def test_settings_show_and_save_offer_and_rarity_settings(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    with patch.object(DashboardRequestHandler, "config", cfg, create=True), patch.object(DashboardRequestHandler, "evaluator", ev, create=True):
        status, s = _call("GET", "/api/settings", cfg, store, ev, cfg_path)
        tf = s["rules"]["trading_frequency"]
        assert (tf["min_offer_sales_14d"], tf["rare_item_pct"]) == (1, 10.0)
        status, r = _call("POST", "/api/settings", cfg, store, ev, cfg_path,
                          {"rules": {"trading_frequency": {"enabled": True, "value": 2, "min_offer_sales_14d": 2,
                                                           "rare_item_pct": 5}}})
        assert status == 200 and r["success"]
        saved = yaml.safe_load((tmp_path / "overrides.yaml").read_text())["filters"]["trading_frequency"]
        assert saved == {"min_offer_sales_14d": 2, "rare_item_pct": 5.0}
        loaded = load_config(cfg_path).filters.trading_frequency
        assert (loaded.min_offer_sales_14d, loaded.rare_item_pct) == (2, 5.0)
        status, r = _call("POST", "/api/settings", cfg, store, ev, cfg_path,
                          {"rules": {"trading_frequency": {"rare_item_pct": 150}}})
        assert status == 400


# ---------------------------------------------------------------------------
# Candidates: each collection's latest check
# ---------------------------------------------------------------------------
def test_latest_result_follows_every_check_even_after_a_pass(tmp_path):
    store = StateStore(str(tmp_path / "bot.db"))
    store.add_discovered_slugs(["a", "b"])
    store.record_candidate("a", "2026-10-05", is_pass=True, reasons="PASS", details={})
    store.record_candidate("b", "2026-10-06", is_pass=True, reasons="PASS", details={})
    store.record_candidate("b", "2026-10-06", is_pass=False, reasons="trading_frequency: x",
                           reject_filter="trading_frequency", details={"reason": "No sales at floor price in the last 7 days"})
    store.mark_collection_evaluated("a")
    latest = store.get_latest_results(["a", "b", "missing"])
    assert latest["a"]["pass"] is True and latest["a"]["last_checked"]
    assert latest["b"] == {**latest["b"], "pass": False, "rule": "trading_frequency",
                           "reason": "No sales at floor price in the last 7 days"}
    assert store.get_result("b", "2026-10-06")["is_pass"] is True   # the day's pass (and its Info.md) is kept
    assert "missing" not in latest


def test_candidates_endpoint_sends_latest_and_home_counts_only_still_passing(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    store.add_discovered_slugs(["a", "b"])
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    store.record_candidate("a", today, is_pass=True, reasons="PASS", details={})
    store.record_candidate("b", today, is_pass=True, reasons="PASS", details={})
    store.record_candidate("b", "2099-01-01", is_pass=False, reasons="x", reject_filter="net_profit", details={})
    with patch.object(DashboardRequestHandler, "config", cfg, create=True), patch.object(DashboardRequestHandler, "evaluator", ev, create=True):
        status, r = _call("GET", "/api/candidates?days=7", cfg, store, ev, cfg_path)
        assert status == 200 and set(r["latest"]) == {"a", "b"} and r["latest"]["b"]["pass"] is False
        status, o = _call("GET", "/api/overview", cfg, store, ev, cfg_path)
        assert status == 200 and o["candidates_7d"] == 1
        # Home also says how many passed this week and how many Info.md files that left on disk
        assert o["passed_7d"] == 2 and o["dossiers_7d"] == 0
        for slug in ("a", "b"):
            d = os.path.join(cfg.general.data_root, today, slug)
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "Info.md"), "w").close()
        status, o = _call("GET", "/api/overview", cfg, store, ev, cfg_path)
        assert o["dossiers_7d"] == 2


def test_review_fixes_old_sales_trait_shape_and_passing_again(tmp_path):
    # Compared only with today's floor, a sale at 78% may be a listing bought before the floor rose: not counted
    assert offers([sale(0, 0.78)]).offer_sales == 0
    assert offers([sale(0, 0.70)]).offer_sales == 1
    # Older criteria shape with a single trait is a trait offer
    old = _order(1, {"collection": {"slug": "x"}, "trait": {"type": "Hat", "value": "Cap"}, "encoded_token_ids": None},
                 {"itemType": 4, "identifierOrCriteria": "55"})
    assert parse_order_info(old)["offer_type"] == "trait"
    # Pass, fail, pass again on the same day: the latest result is a pass
    store = StateStore(str(tmp_path / "bot.db"))
    store.add_discovered_slugs(["a"])
    store.record_candidate("a", "2026-10-06", is_pass=True, reasons="PASS", details={})
    store.record_candidate("a", "2026-10-06", is_pass=False, reasons="x", reject_filter="floor_change_1d", details={})
    store.set_last_result_pass("a")
    assert store.get_latest_results(["a"])["a"]["pass"] is True


def test_failed_older_download_is_not_a_near_miss_and_rarity_misses_wait_a_day():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        week = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2)
        ev.provider.get_sale_events.side_effect = lambda slug, after_timestamp, before_timestamp=None, **kw: (
            None if before_timestamp else week)
        ev.provider.get_nft_rarity.return_value = (False, None)
        assert ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True) is None
        det = store.get_results_since("2000-01-01")[0]["details"]
        assert det["limit"] is None and "API failed" in det["reason"]
        calls = ev.provider.get_nft_rarity.call_count
        assert calls > 0
        ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert ev.provider.get_nft_rarity.call_count == calls   # the miss is remembered


def test_offer_sales_list_each_sale_with_how_it_was_judged_and_a_link():
    events = [sale(0, 0.6), sale(1, 0.62), sale(2, 0.65), sale(3, 0.98)]
    info = {"0xo0": {"kind": "offer", "offer_type": "collection"},
            "0xo1": {"kind": "offer", "offer_type": "trait"},
            "0xo3": {"kind": "listing"}}                       # 0xo2: no order record, judged by price
    m = offers(events, lambda e: info.get(e.order_hash))
    rows = m.summary(1)["rows"]
    assert [r["how"] for r in sorted(rows, key=lambda r: r["ts"])] == ["confirmed", "other", "price"]
    first = next(r for r in rows if r["how"] == "confirmed")
    assert first["url"] == "https://opensea.io/item/ethereum/0xC/0" and first["pct"] == 60.0
    assert next(r for r in rows if r["how"] == "other")["offer_type"] == "trait"
    assert m.counted_ids == {"e0": "confirmed", "e2": "price"}


def test_a_confirmed_offer_shows_as_accepted_offer_in_last_weeks_table():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        week = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2, offer_price=0.7)
        for e in week:
            e.protocol_address = "0xseaport"
        ev.provider.get_sale_events.return_value = week
        ev.provider.get_order_info.side_effect = lambda chain, proto, h: (
            {"kind": "offer", "offer_type": "collection"} if h == week[0].order_hash else {"kind": "listing", "offer_type": None})
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.is_overall_pass
        det = ev.last_details
        offer_row = next(r for r in det["floor_sales"]["rows"] if r["ts"] == week[0].event_timestamp)
        assert (offer_row["label"], offer_row["how"]) == ("below", "offer")
        assert det["offer_sales"]["rows"][0]["how"] == "confirmed"
        assert det["offer_sales"]["rows"][0]["url"].startswith("https://opensea.io/item/ethereum/")


def test_offer_list_keeps_counted_sales_first_and_says_how_many_are_left_out():
    from src.metrics.floor_sales import MAX_ROWS
    n_other = MAX_ROWS + 5
    events = [sale(i, 0.6) for i in range(n_other + 3)]
    # The 3 oldest are collection offers, the newer ones trait offers
    info = lambda e: {"kind": "offer", "offer_type": "collection" if int(e.event_id[1:]) < 3 else "trait"}
    m = offers(events, info)
    s = m.summary(1)
    assert s["row_count"] == n_other + 3 and len(s["rows"]) == MAX_ROWS
    assert sum(r["how"] == "confirmed" for r in s["rows"]) == 3
    assert [r["ts"] for r in s["rows"]] == sorted((r["ts"] for r in s["rows"]), reverse=True)
    assert set(m.offer_ids.values()) == {"confirmed", "other"}


def test_trait_offer_below_floor_reads_as_accepted_offer_and_failed_older_week_is_flagged():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        week = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=1.2, offer_price=0.7)
        for e in week:
            e.protocol_address = "0xseaport"
        ev.provider.get_sale_events.side_effect = lambda slug, after_timestamp, before_timestamp=None, **kw: (
            None if before_timestamp else week)
        ev.provider.get_order_info.side_effect = lambda chain, proto, h: (
            {"kind": "offer", "offer_type": "trait"} if h == week[0].order_hash else {"kind": "listing", "offer_type": None})
        ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=False)
        det = ev.last_details
        row = next(r for r in det["floor_sales"]["rows"] if r["ts"] == week[0].event_timestamp)
        assert (row["label"], row["how"]) == ("below", "offer")
        assert det["offer_sales"]["covered_days"] == 7 and det["offer_sales"]["days"] == 14
