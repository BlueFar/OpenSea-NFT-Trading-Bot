import json
import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.config.settings import load_config
from src.models.collection import CollectionMetadata, CollectionStats, Contract, Fee, Offer, SaleEvent
from src.simulation.engine import (
    Costs, PaperTrader, break_even_ask, decide_bid, describe_position, gap_pct, max_bid_for_gap,
    outbid_price, round_down, target_ask, trade_result,
)
from src.simulation.store import SimStore
from src.storage.state_store import StateStore
from src.trade_model.calculator import compute_trade_economics

OPENSEA_FEE_WALLET = "0x0000a26b00c1f0df003000390027140000faa719"
COSTS = Costs(fee_pct=1.0, royalty_pct=5.0, gas=0.005, exit_discount_pct=5.0)


# ----------------------------------------------------------------------
# Pricing rules
# ----------------------------------------------------------------------
def _bot_spread(floor, offer, royalty_pct=5.0):
    """The bot's own "Spread over top offer" for an offer placed at exactly `offer`."""
    coll = CollectionMetadata(slug="x", name="x", fees=[Fee(royalty_pct, "0xcreator", True), Fee(1.0, OPENSEA_FEE_WALLET, True)])
    te = compute_trade_economics(current_floor=floor, observed_top_offer=offer, floor_currency="ETH", top_offer_currency="WETH",
                                 collection=coll, entry_offer_premium_pct=0.0, target_sale_discount_from_floor_pct=5.0)
    return te.modelled.floor_premium_over_effective_offer_pct


def test_spread_and_limit_match_the_bots_offer_to_floor_rule():
    for floor, royalty in ((0.1088, 5.0), (0.0398, 10.0), (4.96, 5.0), (0.005, 0.0)):
        costs = Costs(1.0, royalty, 0.005, 5.0)
        assert gap_pct(floor, 0.5 * floor, costs) == pytest.approx(_bot_spread(floor, 0.5 * floor, royalty))
        limit = max_bid_for_gap(floor, costs, 40.0)
        assert _bot_spread(floor, limit, royalty) == pytest.approx(40.0)
        assert _bot_spread(floor, round_down(limit), royalty) >= 40.0


def test_outbid_is_the_smallest_step_above_the_best_offer():
    for top in (0.0596, 0.0197, 0.003, 0.0024, 3.01, 0.9999, 12.5):
        bid = outbid_price(top, 0.1)
        assert top < bid <= top * 1.002
    assert outbid_price(0.0596, 0.1) == 0.05966


def test_decide_bid_states():
    floor = 0.1088
    limit = max_bid_for_gap(floor, COSTS, 40.0)  # ~0.0725
    assert decide_bid(0.0596, floor, COSTS, 40.0, 0.1, None) == (0.05966, "top", limit)
    bid, state, _ = decide_bid(0.09, floor, COSTS, 40.0, 0.1, 0.05966)
    assert state == "capped" and bid == round_down(limit) and bid < 0.09
    # Just under the limit: still the best offer, at the limit
    bid, state, _ = decide_bid(limit * 0.9995, floor, COSTS, 40.0, 0.1, None)
    assert state == "top" and limit * 0.9995 < bid <= limit
    # Nobody else bidding: keep ours, or open at half the floor
    assert decide_bid(None, floor, COSTS, 40.0, 0.1, 0.06)[:2] == (0.06, "alone")
    assert decide_bid(None, floor, COSTS, 40.0, 0.1, None)[:2] == (0.0544, "alone")
    # No floor this check: leave the offer alone
    assert decide_bid(0.05, None, COSTS, 40.0, 0.1, 0.06)[:2] == (0.06, "no_floor")
    # A royalty so high that no offer keeps the spread
    assert decide_bid(0.05, floor, Costs(1.0, 80.0, 0.0, 5.0), 40.0, 0.1, 0.06)[:2] == (None, "paused")


def test_listing_follows_the_floor_but_never_below_break_even():
    entry = 0.06
    be = break_even_ask(entry, COSTS)
    assert trade_result(entry, be, COSTS)["net"] == pytest.approx(0.0, abs=1e-12)
    assert target_ask(0.1088, entry, COSTS, None) == 0.1034           # 5% under the floor, rounded up
    assert target_ask(0.06, entry, COSTS, None) >= be                  # floor fell: hold at break-even
    assert target_ask(None, entry, COSTS, 0.1034) == 0.1034            # no floor: keep the listing


# ----------------------------------------------------------------------
# The simulator, against a scripted market
# ----------------------------------------------------------------------
class FakeMarket:
    def __init__(self):
        self.floor, self.top, self.sales, self.orders = 0.1088, 0.0596, [], {}
        self.coll = CollectionMetadata(slug="kongz", name="Kongz Babies", contracts=[Contract("0xabc", "ethereum")],
                                       fees=[Fee(5.0, "0xcreator", True), Fee(1.0, OPENSEA_FEE_WALLET, True)],
                                       raw_data={"image_url": "https://img/x.png"})
        self.order_lookups = 0

    def get_collection(self, slug):
        return self.coll

    def get_collection_stats(self, slug, max_age=300.0):
        return CollectionStats(floor_price=self.floor, floor_price_symbol="ETH")

    def get_top_offer(self, slug, currency_filter=None):
        if self.top is None or (currency_filter and not currency_filter("WETH")):
            return None
        return Offer(order_hash="o", chain="ethereum", price_value=self.top, price_currency="WETH")

    def get_sale_events(self, slug, after_timestamp, max_pages=3, **kw):
        return [e for e in self.sales if e.event_timestamp >= after_timestamp]

    def get_order_info(self, chain, protocol, order_hash):
        self.order_lookups += 1
        return self.orders.get(order_hash)

    def sale(self, ts, price, token="7", currency="WETH", order_hash=None):
        ev = SaleEvent(event_id=f"e{len(self.sales)}", chain="ethereum", event_timestamp=ts, transaction=f"0xtx{len(self.sales)}",
                       order_hash=order_hash, token_id=token, price_value=price, price_currency=currency,
                       protocol_address="0xseaport")
        self.sales.append(ev)
        return ev


def _trader(tmp_path, market, **sim):
    cfg = load_config("config/config.yaml")
    cfg.general.state_db_path = str(tmp_path / "bot.db")
    cfg.simulation.collections = ["kongz"]
    cfg.simulation.final_list = {}
    cfg.simulation.include_candidates = False
    for k, v in sim.items():
        setattr(cfg.simulation, k, v)
    state = StateStore(cfg.general.state_db_path)
    return PaperTrader(market, state, cfg), state


def test_full_round_trip_bid_outbid_cap_fill_list_sell(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    s = trader.store
    t0 = int(time.time()) - 20000
    m.sale(t0 - 100, 0.05)  # before we bid: never ours

    trader.tick(now=t0)
    p = s.get_position("kongz")
    assert (p["status"], p["bid"], p["bid_state"], p["name"], p["fee_pct"], p["royalty_pct"], p["gas"]) == \
        ("bidding", 0.05966, "top", "Kongz Babies", 1.0, 5.0, 0.005)

    m.top = 0.0610                     # someone beats us
    trader.tick(now=t0 + 300)
    p = s.get_position("kongz")
    assert p["bid"] == 0.06107 and p["times_outbid"] == 1

    m.top = 0.09                       # above our limit: hold at the limit
    trader.tick(now=t0 + 600)
    p = s.get_position("kongz")
    assert p["bid_state"] == "capped" and p["bid"] == round_down(p["max_bid"]) and p["bid"] < 0.09
    capped_bid = p["bid"]

    m.sale(t0 + 700, 0.09)             # sold into the higher offer, not ours
    m.top = 0.06                       # that offer is gone: back on top
    trader.tick(now=t0 + 900)
    p = s.get_position("kongz")
    assert p["status"] == "bidding" and p["bid"] == 0.06006 and p["bid_state"] == "top"
    assert capped_bid > 0.06006

    m.sale(t0 + 1000, 0.06, token="42")  # a seller took 0.06: our 0.06006 would have been taken
    trader.tick(now=t0 + 1200)
    p = s.get_position("kongz")
    assert (p["status"], p["entry_price"], p["entry_at"], p["entry_token"], p["ask"]) == ("holding", 0.06006, t0 + 1000, "42", 0.1034)

    m.sale(t0 + 1300, 0.20)            # a rare item, far above the floor: not a floor buyer
    m.sale(t0 + 1350, 0.1000)          # someone bought a cheaper listing than ours
    trader.tick(now=t0 + 1500)
    assert s.get_position("kongz")["status"] == "holding"

    m.sale(t0 + 1600, 0.105)           # a buyer paid more than our listing: ours would have sold
    trader.tick(now=t0 + 1800)
    p = s.get_position("kongz")
    assert (p["status"], p["round"], p["bid"], p["entry_price"]) == ("bidding", 2, None, None)
    [trade] = s.get_trades()
    expect = trade_result(0.06006, 0.1034, COSTS)
    assert trade["entry_price"] == 0.06006 and trade["exit_price"] == 0.1034 and trade["exit_at"] == t0 + 1600
    assert trade["net"] == pytest.approx(expect["net"]) and trade["roi_pct"] == pytest.approx(expect["roi_pct"])
    assert trade["times_outbid"] == 2  # raised once, then capped once

    # Round 2 bids again at the next check; nothing from round 1 is judged twice
    trader.tick(now=t0 + 2100)
    p = s.get_position("kongz")
    assert p["round"] == 2 and p["bid"] == 0.06006 and p["status"] == "bidding"
    assert len(s.get_trades()) == 1
    kinds = [e["kind"] for e in reversed(s.get_events())]
    assert kinds == ["added", "placed", "raised", "capped", "top", "filled", "listed", "sold", "placed"]


def test_far_below_our_offer_counts_only_if_it_was_an_accepted_offer(tmp_path):
    m = FakeMarket()
    trader, state = _trader(tmp_path, m)
    t0 = int(time.time()) - 20000
    trader.tick(now=t0)
    m.sale(t0 + 100, 0.01, order_hash="0xprivate")      # a private sale for next to nothing
    m.orders["0xprivate"] = {"kind": "listing", "offer_type": None}
    trader.tick(now=t0 + 300)
    assert trader.store.get_position("kongz")["status"] == "bidding"
    assert state.get_sale_orders(["0xprivate"])["0xprivate"]["kind"] == "listing"

    m.sale(t0 + 400, 0.03, order_hash="0xbid")          # a seller accepted a low item offer
    m.orders["0xbid"] = {"kind": "offer", "offer_type": "item"}
    trader.tick(now=t0 + 600)
    p = trader.store.get_position("kongz")
    assert p["status"] == "holding" and p["entry_price"] == 0.05966
    assert m.order_lookups == 2


def test_sales_in_another_currency_and_repeats_are_ignored(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    t0 = int(time.time()) - 20000
    trader.tick(now=t0)
    m.sale(t0 + 100, 0.05, currency="USDC")
    trader.tick(now=t0 + 300)
    trader.tick(now=t0 + 600)  # the same sale comes back in the look-back window
    assert trader.store.get_position("kongz")["status"] == "bidding"


def test_no_repeat_stops_after_selling(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m, repeat=False)
    t0 = int(time.time()) - 20000
    trader.tick(now=t0)
    m.sale(t0 + 100, 0.0596)
    trader.tick(now=t0 + 300)
    m.sale(t0 + 400, 0.11)
    trader.tick(now=t0 + 600)
    p = trader.store.get_position("kongz")
    assert p["status"] == "stopped" and len(trader.store.get_trades()) == 1
    trader.tick(now=t0 + 900)
    assert trader.store.get_position("kongz")["last_tick_at"] == t0 + 600


def test_stop_pressed_during_a_check_is_not_overwritten(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    t0 = int(time.time()) - 20000
    trader.tick(now=t0)
    stale = trader.store.get_position("kongz")
    trader.store.set_status("kongz", "stop")       # pressed on the dashboard mid-check
    m.top = 0.0610
    trader._step(stale, t0 + 300)
    p = trader.store.get_position("kongz")
    assert p["status"] == "stopped" and p["bid"] == 0.05966
    trader.tick(now=t0 + 600)                      # stopped positions are left alone
    assert trader.store.get_position("kongz")["bid"] == 0.05966

    trader.store.set_status("kongz", "start")
    trader.tick(now=t0 + 900)
    p = trader.store.get_position("kongz")
    assert p["status"] == "bidding" and p["bid"] == 0.06107


def test_removed_collection_is_not_added_back_from_the_settings_file(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    trader.tick(now=int(time.time()) - 600)
    trader.store.set_status("kongz", "remove")
    trader.tick()
    assert trader.store.get_positions() == []
    assert trader.store.get_position("kongz")["status"] == "removed"


def test_one_collection_failing_does_not_stop_the_others(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    trader.config.simulation.collections = ["broken", "kongz"]
    real = m.get_collection
    m.get_collection = lambda slug: None if slug == "broken" else real(slug)
    assert trader.tick(now=int(time.time()) - 600) == 1
    assert "Couldn't load" in trader.store.get_position("broken")["note"]
    assert trader.store.get_position("kongz")["bid"] == 0.05966
    n_events = len(trader.store.get_events())
    trader.tick(now=int(time.time()) - 300)          # the same problem isn't logged again
    assert len(trader.store.get_events()) == n_events


def test_describe_position_profit_figures():
    pos = {"status": "bidding", "fee_pct": 1.0, "royalty_pct": 5.0, "gas": 0.005, "floor": 0.1088, "bid": 0.05966,
           "entry_price": None}
    d = describe_position(pos, 5.0)
    assert d["gap_pct"] == pytest.approx(_bot_spread(0.1088, 0.05966))
    assert d["est_net"] == pytest.approx(trade_result(0.05966, 0.1088 * 0.95, COSTS)["net"])
    held = describe_position({**pos, "status": "holding", "entry_price": 0.06, "ask": 0.1034}, 5.0)
    assert held["ask_net"] == pytest.approx(trade_result(0.06, 0.1034, COSTS)["net"])
    assert held["break_even"] == pytest.approx(break_even_ask(0.06, COSTS))
    assert "gap_pct" not in held


# ----------------------------------------------------------------------
# Bot loop and dashboard
# ----------------------------------------------------------------------
def test_bot_runs_the_simulation_on_its_own_schedule(tmp_path):
    from src.bot import NFTBot
    cfg = load_config("config/config.yaml")
    cfg.general.state_db_path = str(tmp_path / "bot.db")
    with patch("src.bot.signal.signal"):
        bot = NFTBot(cfg)
    bot.simulator.tick = MagicMock(return_value=5)
    bot._maybe_run_simulation()
    bot._maybe_run_simulation()                      # not due yet
    assert bot.simulator.tick.call_count == 1
    bot._last_sim_time -= cfg.simulation.interval_seconds
    bot._maybe_run_simulation()
    assert bot.simulator.tick.call_count == 2
    bot.simulator.tick.side_effect = RuntimeError("boom")
    bot._last_sim_time = 0
    bot._maybe_run_simulation()                      # never raises into the bot loop
    cfg.simulation.enabled = False
    bot._last_sim_time = 0
    bot._maybe_run_simulation()
    assert bot.simulator.tick.call_count == 3


def _dash_env(tmp_path):
    from tests.test_mac_runtime_and_chains import _settings_env
    return _settings_env(tmp_path)


def _call(method, path, env, body=None):
    from tests.test_mac_runtime_and_chains import _call as call
    cfg_path, cfg, store, ev = env
    return call(method, path, cfg, store, ev, cfg_path, body)


def test_dashboard_simulation_api(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    env = _dash_env(tmp_path)
    cfg_path, cfg, store, ev = env
    DashboardRequestHandler.sim_store = None
    with patch.object(DashboardRequestHandler, "config", cfg, create=True), \
         patch.object(DashboardRequestHandler, "evaluator", ev, create=True):
        status, r = _call("GET", "/api/simulation", env)
        assert status == 200 and r["positions"] == [] and r["min_profit_gap_pct"] == 40.0 and r["exit_discount_pct"] == 5.0

        status, r = _call("POST", "/api/simulation/add", env, {"slug": "https://opensea.io/collection/Smol-Brains?tab=items"})
        assert status == 200 and r["position"]["slug"] == "smol-brains" and r["position"]["status"] == "bidding"
        status, r = _call("POST", "/api/simulation/add", env, {"slug": "smol-brains"})
        assert status == 409

        ev.provider.get_collection.return_value = None
        status, r = _call("POST", "/api/simulation/add", env, {"slug": "nope"})
        assert status == 404

        sim = SimStore(cfg.general.state_db_path)
        pos = sim.get_position("smol-brains")
        pos.update({"name": "Smol Brains", "currency": "ETH", "fee_pct": 1.0, "royalty_pct": 5.0, "gas": 0.0002,
                    "floor": 0.005, "bid": 0.003003, "bid_state": "top", "competitor_top": 0.003})
        assert sim.save_round(pos, pos["rev"], ticks=[("smol-brains", int(time.time()) - 60, 1, "bidding", 0.003003, 0.003, 0.005, None)])
        status, r = _call("GET", "/api/simulation", env)
        [p] = r["positions"]
        assert p["name"] == "Smol Brains" and p["gap_pct"] > 40 and p["est_net"] > 0
        assert len(r["charts"]["smol-brains"]) == 1 and r["events"][0]["name"] == "Smol Brains"
        assert p["last_event"]["kind"] == "added"

        assert _call("POST", "/api/simulation/stop", env, {"slug": "smol-brains"})[1]["position"]["status"] == "stopped"
        assert _call("POST", "/api/simulation/start", env, {"slug": "smol-brains"})[1]["position"]["status"] == "bidding"
        assert _call("POST", "/api/simulation/remove", env, {"slug": "smol-brains"})[1]["position"]["status"] == "removed"
        assert _call("GET", "/api/simulation", env)[1]["positions"] == []
        assert _call("POST", "/api/simulation/stop", env, {"slug": "ghost"})[0] == 404
        assert _call("POST", "/api/simulation/nuke", env, {"slug": "smol-brains"})[0] == 404


def test_simulation_settings_are_in_the_config_file():
    raw = yaml.safe_load(open("config/config.yaml", encoding="utf-8"))
    assert raw["simulation"]["collections"] == ["cyberkongz-babies", "buckets-club-front-nine", "smol-brains",
                                                "sleap-io-city-collection-base", "chicken-derby"]
    assert raw["simulation"]["min_profit_gap_pct"] == 40.0


# ----------------------------------------------------------------------
# Cases found in review
# ----------------------------------------------------------------------
def _holding(tmp_path, **sim):
    """A position that bought at 0.05966 and listed at 0.1034 (floor 0.1088)."""
    m = FakeMarket()
    trader, state = _trader(tmp_path, m, **sim)
    t0 = int(time.time()) - 20000
    trader.tick(now=t0)
    m.sale(t0 + 100, 0.0596)
    trader.tick(now=t0 + 300)
    p = trader.store.get_position("kongz")
    assert p["status"] == "holding" and p["ask"] == 0.1034
    return m, trader, state, t0


def test_sales_made_while_stopped_never_count(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    t0 = int(time.time()) - 20000
    trader.tick(now=t0)
    trader.store.set_status("kongz", "stop")
    m.sale(t0 + 1000, 0.0596)                       # would have filled the old offer
    trader.store.set_status("kongz", "start")
    trader.tick(now=t0 + 1300)
    trader.tick(now=t0 + 1600)
    assert trader.store.get_position("kongz")["status"] == "bidding"

    m2, trader2, _, t1 = _holding(tmp_path / "h")
    trader2.store.set_status("kongz", "stop")
    m2.sale(t1 + 1000, 0.105)                       # would have bought the old listing
    trader2.store.set_status("kongz", "start")
    trader2.tick(now=t1 + 1300)
    trader2.tick(now=t1 + 1600)
    assert trader2.store.get_position("kongz")["status"] == "holding" and trader2.store.get_trades() == []


def test_a_listing_held_at_break_even_above_the_floor_only_sells_once_the_floor_reaches_it(tmp_path):
    m, trader, _, t0 = _holding(tmp_path)
    m.floor = 0.062                                 # break-even is ~0.0688
    trader.tick(now=t0 + 600)
    ask = trader.store.get_position("kongz")["ask"]
    assert ask > m.floor
    m.sale(t0 + 700, 0.070)                         # someone picked a particular item above the floor
    trader.tick(now=t0 + 900)
    assert trader.store.get_trades() == []
    m.floor = 0.075                                 # the cheaper listings sold: ours is now under the floor
    m.sale(t0 + 1000, 0.075)
    trader.tick(now=t0 + 1200)
    [t] = trader.store.get_trades()
    assert t["exit_price"] == ask and t["net"] >= 0


def test_wash_trades_and_item_offer_accepts_are_not_buyers(tmp_path):
    m, trader, _, t0 = _holding(tmp_path)
    ev = m.sale(t0 + 400, 0.108)
    ev.seller = ev.buyer = "0xAAA"                  # trading with yourself
    m.sale(t0 + 450, 0.106, order_hash="0xtrait")   # a seller accepted a trait offer
    m.orders["0xtrait"] = {"kind": "offer", "offer_type": "trait"}
    trader.tick(now=t0 + 600)
    assert trader.store.get_trades() == []
    m.sale(t0 + 700, 0.106, order_hash="0xbuy")     # a buyer bought a listing
    m.orders["0xbuy"] = {"kind": "listing", "offer_type": None}
    trader.tick(now=t0 + 900)
    assert len(trader.store.get_trades()) == 1


def test_currency_waits_for_a_real_floor(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    real = m.get_collection_stats
    m.get_collection_stats = lambda slug, max_age=300.0: CollectionStats(floor_price=None, floor_price_symbol="ETH")
    trader.tick(now=int(time.time()) - 900)
    p = trader.store.get_position("kongz")
    assert p["currency"] is None and p["bid"] is None and "No floor price" in p["note"]
    m.get_collection_stats = real
    trader.tick(now=int(time.time()) - 600)
    p = trader.store.get_position("kongz")
    assert p["currency"] == "ETH" and p["bid"] == 0.05966 and p["note"] is None


def test_floor_moves_while_capped_are_not_counted_as_outbids(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    t0 = int(time.time()) - 20000
    trader.tick(now=t0)
    m.top = 0.09
    trader.tick(now=t0 + 300)
    for i, f in enumerate((0.110, 0.108, 0.111, 0.107)):
        m.floor = f
        trader.tick(now=t0 + 600 + 300 * i)
    p = trader.store.get_position("kongz")
    assert p["bid_state"] == "capped" and p["times_outbid"] == 1


def test_a_sale_waiting_for_an_order_look_up_is_judged_at_the_next_check(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    t0 = int(time.time()) - 20000
    trader.tick(now=t0)
    for i in range(5):                              # five private sales use up this check's look-ups
        m.sale(t0 + 100 + i, 0.01, order_hash=f"0xp{i}")
        m.orders[f"0xp{i}"] = {"kind": "listing", "offer_type": None}
    m.sale(t0 + 200, 0.03, order_hash="0xbid")      # a seller accepted a low collection offer
    m.orders["0xbid"] = {"kind": "offer", "offer_type": "collection"}
    trader.tick(now=t0 + 300)
    assert trader.store.get_position("kongz")["status"] == "bidding"
    trader.tick(now=t0 + 600)
    p = trader.store.get_position("kongz")
    assert p["status"] == "holding" and p["entry_at"] == t0 + 200


def test_a_failed_sales_download_is_retried_and_a_truncated_one_is_flagged(tmp_path):
    m = FakeMarket()
    trader, _ = _trader(tmp_path, m)
    t0 = int(time.time()) - 20000
    trader.tick(now=t0)
    m.get_sale_events_since = lambda slug, after_timestamp, max_pages=10: (None, False)
    m.sale(t0 + 100, 0.0596)
    trader.tick(now=t0 + 300)
    p = trader.store.get_position("kongz")
    assert p["status"] == "bidding" and p["last_sale_check"] == t0
    m.get_sale_events_since = lambda slug, after_timestamp, max_pages=10: (list(m.sales), False)
    trader.tick(now=t0 + 600)
    p = trader.store.get_position("kongz")
    assert p["status"] == "holding" and p["last_sale_check"] == t0 + 600
    assert any(e["kind"] == "problem" and "1,000" in e["message"] for e in trader.store.get_events())


def test_removing_a_held_item_keeps_it_in_the_results(tmp_path):
    from src.simulation.engine import closing_trade
    m, trader, _, t0 = _holding(tmp_path)
    m.floor = 0.05
    trader.tick(now=t0 + 600)
    pos = trader.store.get_position("kongz")
    trader.store.set_status("kongz", "remove", closing_trade=closing_trade(pos, 5.0))
    [t] = trader.store.get_trades()
    assert t["how"] == "removed" and t["exit_price"] == pytest.approx(0.0475) and t["net"] < 0
    trader.store.add_position("kongz")
    p = trader.store.get_position("kongz")
    assert p["status"] == "bidding" and p["entry_price"] is None


def _passing(state, slug, passes=True):
    with sqlite3.connect(state.db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO monitored_collections (slug, discovery_source) VALUES (?, 'test')", (slug,))
        conn.execute("UPDATE monitored_collections SET last_result_pass=? WHERE slug=?", (1 if passes else 0, slug))


def test_candidates_join_and_leave_the_simulation_with_the_bots_checks(tmp_path):
    m = FakeMarket()
    t, state = _trader(tmp_path, m, collections=[], include_candidates=True)
    _passing(state, "alpha")
    _passing(state, "beta")
    t.seed()
    assert {p["slug"]: p["source"] for p in t.store.get_positions()} == {"alpha": "candidate", "beta": "candidate"}

    # Stops passing: out of the simulation at once
    _passing(state, "beta", passes=False)
    t.seed()
    assert t.store.get_position("beta")["status"] == "removed"
    assert t.store.get_position("alpha")["status"] == "bidding"

    # Passes again: back in
    _passing(state, "beta")
    t.seed()
    assert t.store.get_position("beta")["status"] == "bidding"

    # Stopped by hand: stays stopped while it passes
    t.store.set_status("alpha", "stop")
    t.seed()
    assert t.store.get_position("alpha")["status"] == "stopped"
    # Removed by hand: not added back
    t.store.set_status("beta", "remove")
    t.seed()
    assert t.store.get_position("beta")["status"] == "removed"


def test_a_candidate_that_stops_passing_while_holding_leaves_after_the_sale(tmp_path):
    m, t, state, t0 = _holding(tmp_path)
    t.sim.collections, t.sim.include_candidates = [], True
    _passing(state, "kongz")  # (bought while it passed)
    with sqlite3.connect(state.db_path) as c:
        c.execute("UPDATE sim_positions SET source='candidate' WHERE slug='kongz'")
    _passing(state, "kongz", passes=False)
    t.seed()
    p = t.store.get_position("kongz")
    assert p["status"] == "holding" and p["leaving"] == 1
    m.sale(t0 + 400, 0.11)
    t.tick(now=t0 + 600)
    p = t.store.get_position("kongz")
    assert p["status"] == "removed" and p["entry_price"] is None
    [trade] = t.store.get_trades()
    assert trade["how"] == "sold" and trade["net"] > 0
    # Passes again: comes back with a fresh offer
    _passing(state, "kongz")
    t.seed()
    assert t.store.get_position("kongz")["status"] == "bidding"


def test_final_list_pick_that_stops_passing_is_kept_after_a_profitable_round(tmp_path):
    m, t, state, t0 = _holding(tmp_path, collections=[], include_candidates=True, final_list={"kongz": "pick"})
    _passing(state, "kongz", passes=False)
    t.seed(now=t0 + 350)
    p = t.store.get_position("kongz")
    assert p["trial_state"] == "on" and p["trial_round"] == 1 and p["status"] == "holding"
    assert t.store.get_last_events()["kongz"]["kind"] == "trial"
    m.sale(t0 + 400, 0.11)
    t.tick(now=t0 + 600)
    p = t.store.get_position("kongz")
    assert p["trial_state"] == "kept" and p["status"] == "bidding" and p["round"] == 2
    assert [f["slug"] for f in t.store.get_final_list()] == ["kongz"]
    # Kept: no new trial while it keeps failing
    t.seed(now=t0 + 30 * 86400)
    assert t.store.get_position("kongz")["trial_state"] == "kept"
    # Passing again clears it, so a later fail starts a fresh trial
    _passing(state, "kongz")
    t.seed()
    assert t.store.get_position("kongz")["trial_state"] is None


def test_final_list_pick_whose_trial_round_does_not_finish_in_time_is_removed_everywhere(tmp_path):
    m, t, state, t0 = _holding(tmp_path, collections=[], include_candidates=True, final_list={"kongz": "pick"})
    _passing(state, "kongz", passes=False)
    t.seed(now=t0 + 350)
    with sqlite3.connect(state.db_path) as c:  # the floor fell: the item is now worth a loss
        c.execute("UPDATE sim_positions SET floor=0.06 WHERE slug='kongz'")
    t.seed(now=t0 + 350 + 6 * 86400)
    assert t.store.get_position("kongz")["status"] == "holding"
    t.seed(now=t0 + 350 + 7 * 86400)
    assert t.store.get_position("kongz")["status"] == "removed"
    assert t.store.get_final_list() == []
    [trade] = t.store.get_trades()
    assert trade["how"] == "removed"  # the bought item still counts in the results, at the floor
    assert "Trial failed" in t.store.get_last_events()["kongz"]["message"]
    # Not added back from the settings file or the Final list seed
    t.seed()
    assert t.store.get_position("kongz")["status"] == "removed"


def test_a_trial_waits_past_the_deadline_for_an_item_worth_a_profit(tmp_path):
    m, t, state, t0 = _holding(tmp_path, collections=[], include_candidates=True, final_list={"kongz": "pick"})
    _passing(state, "kongz", passes=False)
    t.seed(now=t0 + 350)
    with sqlite3.connect(state.db_path) as c:  # bought at 0.0597; the floor rose to 0.2
        c.execute("UPDATE sim_positions SET floor=0.2 WHERE slug='kongz'")
    t.seed(now=t0 + 350 + 8 * 86400)
    p = t.store.get_position("kongz")
    assert p["status"] == "holding" and p["trial_state"] == "on"
    assert [f["slug"] for f in t.store.get_final_list()] == ["kongz"]


def test_final_list_trial_ends_if_it_passes_again(tmp_path):
    m = FakeMarket()
    t, state = _trader(tmp_path, m, collections=[], final_list={"kongz": "pick"})
    _passing(state, "kongz", passes=False)
    t.seed()
    assert t.store.get_position("kongz")["trial_state"] == "on"
    _passing(state, "kongz")
    t.seed()
    assert t.store.get_position("kongz")["trial_state"] is None
    assert t.store.get_last_events()["kongz"]["kind"] == "start"


def test_older_databases_get_the_new_columns(tmp_path):
    db = str(tmp_path / "old.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE sim_positions (slug TEXT PRIMARY KEY, name TEXT, status TEXT, source TEXT, "
                  "auto_stopped INTEGER DEFAULT 0)")
    SimStore(db)
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(sim_positions)")}
    assert {"leaving", "trial_round", "trial_state", "trial_started_at"} <= cols


def test_final_list_is_seeded_once_and_feeds_the_simulation(tmp_path):
    m = FakeMarket()
    t, state = _trader(tmp_path, m, collections=[], final_list={"Smol-Brains": "Strong holders"})
    t.seed()
    [f] = t.store.get_final_list()
    assert f["slug"] == "smol-brains" and f["added_by"] == "claude" and f["note"] == "Strong holders"
    assert t.store.get_position("smol-brains")["source"] == "final"

    # A Final list pick that stops passing the bot's checks keeps bidding
    t.sim.include_candidates = True
    _passing(state, "smol-brains", passes=False)
    t.seed()
    assert t.store.get_position("smol-brains")["status"] == "bidding"

    t.store.remove_final("smol-brains")
    t.seed()
    assert t.store.get_final_list() == []


def test_dashboard_final_list_and_interval(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    env = _dash_env(tmp_path)
    cfg_path, cfg, store, ev = env
    DashboardRequestHandler.sim_store = None
    with patch.object(DashboardRequestHandler, "config", cfg, create=True), \
         patch.object(DashboardRequestHandler, "evaluator", ev, create=True):
        assert _call("POST", "/api/final/add", env, {"slug": ""})[0] == 400
        status, r = _call("POST", "/api/final/add", env, {"slug": "https://opensea.io/collection/Smol-Brains", "note": "my pick"})
        assert status == 200
        assert _call("POST", "/api/final/add", env, {"slug": "smol-brains"})[0] == 409

        sim = SimStore(cfg.general.state_db_path)
        assert sim.get_position("smol-brains")["source"] == "final"
        status, r = _call("GET", "/api/final", env)
        [item] = r["items"]
        assert status == 200 and item["slug"] == "smol-brains" and item["added_by"] == "you" and item["note"] == "my pick"
        assert item["status"] == "bidding" and item["trades"] == 0 and item["realised_net"] is None
        assert _call("GET", "/api/simulation", env)[1]["final"] == ["smol-brains"]

        assert _call("POST", "/api/final/remove", env, {"slug": "smol-brains"})[0] == 200
        assert _call("GET", "/api/final", env)[1]["items"] == []
        assert sim.get_position("smol-brains")["status"] == "bidding"  # still simulated
        assert _call("POST", "/api/final/remove", env, {"slug": "smol-brains"})[0] == 404

        assert _call("POST", "/api/simulation/settings", env, {"interval_minutes": 0})[0] == 400
        assert _call("POST", "/api/simulation/settings", env, {"interval_minutes": "x"})[0] == 400
        status, r = _call("POST", "/api/simulation/settings", env, {"interval_minutes": 5})
        assert status == 200 and r["interval_seconds"] == 300
        assert load_config(str(cfg_path)).simulation.interval_seconds == 300
