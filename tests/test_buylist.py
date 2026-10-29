"""The $50-$500 page (a dollar-filtered view of strategy A) and the Buy List."""
import json
import os
import sqlite3
import tempfile
import time

from src.config.settings import BuyListConfig
from src.simulation.buylist import (BuyListStore, build_band, buy_warnings, entry_key, held_value, market_facts,
                                    readiness)
from src.simulation.store import SimStore

NOW = 1_800_000_000
DAY = 86400
RATES = {"ETH": 2000.0, "WETH": 2000.0, "APE": 0.1}


def rate(cur):
    return RATES.get((cur or "").upper())


def pos(slug, status="bidding", **kw):
    p = {"slug": slug, "name": slug, "image": None, "chain": "ethereum", "currency": "ETH", "status": status,
         "bid": None, "bid_state": "top", "competitor_top": None, "floor": 0.2, "max_bid": None, "times_outbid": 0,
         "fee_pct": 1.0, "royalty_pct": 5.0, "gas": 0.005, "entry_price": None, "entry_at": None, "ask": None,
         "bid_seconds": 10 * DAY, "round": 1, "stop_loss_at": None, "under_since": None, "sl_steps": 0,
         "cool_off_until": None}
    p.update(kw)
    return p


def trade(slug, entry, net, entry_at=NOW - 5 * DAY, exit_at=NOW - 4 * DAY, how="sold", cur="ETH"):
    return {"slug": slug, "currency": cur, "entry_price": entry, "exit_price": entry * 1.5, "entry_at": entry_at,
            "exit_at": exit_at, "net": net, "roi_pct": net / entry * 100, "how": how}


def band(positions, trades, final=(), facts=None, rates=None):
    return build_band(positions, trades, rate, rates or {}, facts or {}, final, [], BuyListConfig(), 5.0, NOW)


def test_band_keeps_only_offers_and_items_between_50_and_500_dollars():
    out = band([pos("cheap", bid=0.01), pos("mid", bid=0.1), pos("dear", bid=0.3),
                pos("apes", currency="APE", bid=100.0)], [])
    assert [b["slug"] for b in out["bids"]] == ["mid"]  # $20, $200, $600, $10


def test_bought_items_use_the_dollar_price_from_when_they_were_bought():
    p = pos("x", status="holding", entry_price=0.2, entry_at=NOW - DAY)
    # Today 0.2 ETH is $400 (in range); bought when ETH was $3,000, it cost $600 (out of range)
    assert band([p], [], rates={entry_key("A", "x", NOW - DAY): 3000.0})["held"] == []
    assert len(band([p], [])["held"]) == 1


def test_money_panel_boxes():
    positions = [pos("a", bid=0.05),  # $100 offer
                 pos("b", status="holding", entry_price=0.1, entry_at=NOW - 2 * DAY, floor=0.2)]
    trades = [trade("c", 0.1, 0.02), trade("d", 0.1, 0.03), trade("e", 0.1, -0.01, how="stoploss_offer"),
              trade("f", 0.1, 0.5, how="removed")]  # a removal is a valuation, not a sale
    m = band(positions, trades)["money"]
    assert m["offers_usd"] == 100.0
    assert m["held_cost_usd"] == 200.0
    assert round(m["win_usd"], 6) == 100.0 and m["wins"] == 2
    assert round(m["loss_usd"], 6) == -20.0 and m["losses"] == 1
    assert round(m["realised_usd"], 6) == 80.0
    assert round(m["after_tied_up_usd"], 6) == 80.0 - 200.0 - 100.0
    # Held item: 0.2 floor, 5% under = 0.19, minus 6% fee and royalty and 0.005 gas = 0.1736 -> $347.20
    assert round(m["held_value_usd"], 2) == 347.2
    assert round(m["total_usd"], 2) == round(80.0 + 347.2 - 200.0, 2)


def test_held_value_uses_the_lower_of_last_sale_and_floor():
    p = pos("x", status="holding", entry_price=0.1, entry_at=NOW, floor=0.3)
    low = held_value(p, {"last_sale": 0.15, "last_sale_currency": "WETH"}, 5.0)
    assert low["basis"] == "last_sale" and low["price"] == 0.15
    high = held_value(p, {"last_sale": 0.5, "last_sale_currency": "ETH"}, 5.0)
    assert high["basis"] == "floor"
    other_coin = held_value(p, {"last_sale": 0.01, "last_sale_currency": "APE"}, 5.0)
    assert other_coin["basis"] == "floor"
    in_sl = held_value(dict(p, stop_loss_at=NOW, ask=0.12), {"last_sale": 0.15, "last_sale_currency": "ETH"}, 5.0)
    assert in_sl["basis"] == "stop_loss" and in_sl["price"] == 0.12


def test_readiness_checklist():
    cfg = BuyListConfig()
    good = [trade("x", 0.1, 0.02), trade("x", 0.1, 0.03)]
    r = readiness(pos("x"), good, True, {"sales_7d": 7}, 200.0, "ethereum", cfg)
    assert r["ready"] and not r["failing"]
    # No time rule: a sale after 3 weeks still counts
    slow = [trade("x", 0.1, 0.02, entry_at=NOW - 30 * DAY, exit_at=NOW - 9 * DAY), trade("x", 0.1, 0.03)]
    assert readiness(pos("x"), slow, True, {"sales_7d": 7}, 200.0, "ethereum", cfg)["ready"]
    # One round is enough after 14 days of bidding
    assert readiness(pos("x", bid_seconds=15 * DAY), good[:1], True, {"sales_7d": 7}, 200.0, "ethereum", cfg)["ready"]
    assert not readiness(pos("x"), good[:1], True, {"sales_7d": 7}, 200.0, "ethereum", cfg)["ready"]
    # A losing round fails the track record
    lost = good + [trade("x", 0.1, -0.01, how="stoploss")]
    assert not readiness(pos("x"), lost, True, {"sales_7d": 7}, 200.0, "ethereum", cfg)["ready"]
    # Each other check
    assert not readiness(pos("x"), good, False, {"sales_7d": 7}, 200.0, "ethereum", cfg)["ready"]
    assert not readiness(pos("x"), good, True, {"sales_7d": 4}, 200.0, "ethereum", cfg)["ready"]
    assert not readiness(pos("x"), good, True, {"sales_7d": 7}, 600.0, "ethereum", cfg)["ready"]
    assert not readiness(pos("x"), good, True, {"sales_7d": 7}, 200.0, "abstract", cfg)["ready"]
    cfg.ready.risky_collections = {"x": "its game's chain is shutting down"}
    r = readiness(None, good, True, {"sales_7d": 7}, 200.0, "ethereum", cfg, slug="x")
    assert not r["ready"] and r["checks"][-1]["detail"] == "its game's chain is shutting down"
    # Unknown is not a pass
    unknown = readiness(pos("x"), good, True, None, None, "ethereum", BuyListConfig())
    assert not unknown["ready"] and {c["key"] for c in unknown["checks"] if c["ok"] is None} == {"band", "sales"}


def test_buy_list_store_and_what_the_bot_may_trade():
    with tempfile.TemporaryDirectory() as d:
        st = BuyListStore(os.path.join(d, "bot.db"))
        assert st.add("a", {"strategy": "A", "min_profit_gap_pct": 40.0}, 0.2, "ETH", True, note="why")
        assert not st.add("a", {}, None, None, False)  # already there
        st.add("b", {}, None, None, False)
        assert st.tradable() == ["a", "b"]
        st.set_enabled("b", False)
        assert st.tradable() == ["a"]
        st.set_paused_all(True)
        assert st.tradable() == [] and st.paused_all()
        st.set_paused_all(False)
        items = {i["slug"]: i for i in st.get_items()}
        assert items["a"]["settings"]["min_profit_gap_pct"] == 40.0 and items["a"]["ready_at_add"]
        assert st.remove("a") and st.slugs() == ["b"]


def test_entry_rates_are_saved_once():
    with tempfile.TemporaryDirectory() as d:
        st = BuyListStore(os.path.join(d, "bot.db"))
        assert st.entry_rates([("A:x:1", 2000.0)]) == {"A:x:1": 2000.0}
        assert st.entry_rates([("A:x:1", 3000.0), ("A:y:2", None)]) == {"A:x:1": 2000.0}


def test_market_facts_from_the_latest_check_that_read_sales():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "bot.db")
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE candidate_history (slug TEXT, date_str TEXT, is_pass INTEGER, reasons TEXT, "
                      "created_at TIMESTAMP, reject_filter TEXT, details TEXT, PRIMARY KEY (slug, date_str))")
            rows = {"total": 6, "rows": [{"label": "floor", "price": 0.11, "ts": 5, "currency": "ETH"},
                                          {"label": "floor", "price": 0.12, "ts": 9, "currency": "ETH"},
                                          {"label": "offer", "price": 0.05, "ts": 10, "currency": "WETH"}]}
            c.execute("INSERT INTO candidate_history VALUES ('x', '2026-10-28', 1, '', 0, NULL, ?)",
                      (json.dumps({"floor_sales": rows}),))
            c.execute("INSERT INTO candidate_history VALUES ('x', '2026-10-29', 0, '', 0, 'floor_change_7d', ?)",
                      (json.dumps({"floor": 0.2}),))  # failed before reading sales
        f = market_facts(db, ["x", "y"])
        assert f["x"]["sales_7d"] == 6 and f["x"]["last_sale"] == 0.12 and f["x"]["checked_on"] == "2026-10-28"
        assert "y" not in f


def test_buy_warnings():
    cfg = BuyListConfig()
    ok = {"ready": True, "failing": [], "checks": []}
    item = {"added_at": NOW - 10 * DAY, "ready_at_add": True, "floor_at_add": 0.2, "currency": "ETH"}
    assert buy_warnings(item, pos("x", floor=0.19), [], ok, cfg, NOW) == []
    w = buy_warnings(item, pos("x", floor=0.15), [], {"ready": False, "failing": ["At least 5 sales"], "checks": []},
                     cfg, NOW)
    assert [x["level"] for x in w] == ["bad", "warn"]  # stopped passing; floor down 25%
    held = pos("x", status="holding", entry_price=0.1, entry_at=NOW - 22 * DAY, stop_loss_at=NOW - DAY)
    texts = " ".join(x["text"] for x in buy_warnings(item, held, [trade("x", 0.1, -0.01, exit_at=NOW - DAY,
                                                                          how="stoploss")], ok, cfg, NOW))
    assert "in a stop-loss" in texts and "stop-loss sale" in texts and "22 days" in texts
    assert "Not in the simulation" in buy_warnings(item, None, [], ok, cfg, NOW)[0]["text"]
