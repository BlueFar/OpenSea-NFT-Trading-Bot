"""Trading pace: at least some of last week's sales were bought at about the floor price, not only accepted offers."""
import tempfile
from unittest.mock import patch

import pytest
import yaml

from src.config.chains import currency_groups
from src.config.settings import load_config
from src.metrics.floor_sales import classify_floor_sales, too_few_floor_sales_reason
from src.models.collection import SaleEvent
from tests.mock_data import make_sample_sales_events
from tests.test_mac_runtime_and_chains import _call, _evaluator, _settings_env

ETH = currency_groups("ethereum")
START, END = 1_000_000, 1_000_000 + 7 * 86400


def sale(i, price, ts=None, cur="ETH", qty=1, tx=None, seller=None, buyer=None):
    return SaleEvent(event_id=f"e{i}", chain="ethereum", event_timestamp=ts or START + 3600 * (i + 1),
                     transaction=tx or f"tx{i}", quantity=qty, price_value=price, price_currency=cur,
                     seller=seller or f"0xs{i}", buyer=buyer or f"0xb{i}")


def classify(events, floor=1.0, snaps=(), mode="transactions"):
    return classify_floor_sales(events, START, END, floor, "ETH", ETH, snaps, count_mode=mode)


def test_band_edges():
    before = [{"ts": START, "floor_price": 1.0, "currency": "ETH"}]
    m = classify([sale(0, 0.899), sale(1, 0.90), sale(2, 1.15), sale(3, 1.151)], snaps=before)
    labels = {r.price: r.label for r in m.rows}
    assert labels == {0.899: "below", 0.90: "floor", 1.15: "floor", 1.151: "above"}
    assert (m.floor_sales, m.below, m.above, m.priced, m.total) == (2, 1, 1, 4, 4)


def test_uses_the_floor_from_just_before_the_sale():
    t = START + 2 * 86400
    snaps = [{"ts": t - 3600, "floor_price": 0.8, "currency": "ETH"},   # before: 0.78 is 97.5% of it
             {"ts": t + 600, "floor_price": 1.0, "currency": "ETH"}]    # after: would make it 78%
    row = classify([sale(0, 0.78, ts=t)], snaps=snaps).rows[0]
    assert (row.ref_source, row.ref_floor, row.label) == ("before", 0.8, "floor")
    # Only a snapshot after the sale
    row = classify([sale(0, 0.97, ts=t)], snaps=snaps[1:]).rows[0]
    assert (row.ref_source, row.label) == ("after", "floor")
    # Nothing within 6 hours: today's floor
    far = [{"ts": t - 7 * 3600, "floor_price": 0.5, "currency": "ETH"}]
    row = classify([sale(0, 0.97, ts=t)], snaps=far).rows[0]
    assert (row.ref_source, row.ref_floor) == ("now", 1.0)
    # A snapshot in another coin is ignored
    usdc = [{"ts": t - 60, "floor_price": 2500.0, "currency": "USDC"}]
    assert classify([sale(0, 0.97, ts=t)], snaps=usdc).rows[0].ref_source == "now"


def test_price_is_per_item_and_coins_must_match():
    m = classify([sale(0, 3.0, qty=3),            # 1.0 each -> floor
                  sale(1, 0.97, cur="WETH"),      # wrapped ETH is the same coin
                  sale(2, 2500.0, cur="USDC"),    # can't compare with an ETH floor
                  sale(3, None)])
    assert [r.label for r in sorted(m.rows, key=lambda r: r.ts)] == ["floor", "floor", "skipped", "skipped"]
    assert m.skipped == 2 and m.floor_sales == 2


def test_wash_trades_are_not_counted():
    m = classify([sale(0, 1.0, seller="0xA", buyer="0xa"),
                  sale(1, 1.0, seller="0xB", buyer="0xC"), sale(2, 1.0, seller="0xC", buyer="0xB"),
                  sale(3, 1.0, seller="0xD", buyer="0xE")])
    assert m.floor_sales == 1 and m.skipped == 3


def test_sweep_counts_once_per_transaction_or_per_item():
    events = [sale(0, 1.0, tx="sweep"), sale(1, 1.0, tx="sweep"), sale(2, 0.5)]
    assert classify(events).floor_sales == 1
    assert classify(events, mode="item_quantity").floor_sales == 2


def test_only_the_seven_full_days_count_and_rows_are_newest_first():
    m = classify([sale(0, 1.0, ts=START - 1), sale(1, 1.0, ts=END + 1), sale(2, 0.5, ts=START + 10), sale(3, 1.0, ts=START + 20)])
    assert m.total == 2 and m.floor_sales == 1
    assert [r.ts for r in m.rows] == [START + 20, START + 10]


def test_unknown_floor_returns_none_and_reasons_read_well():
    assert classify_floor_sales([sale(0, 1.0)], START, END, None, "ETH", ETH) is None
    assert too_few_floor_sales_reason(classify([sale(0, 0.5)]), 1) == "No sales at floor price in the last 7 days"
    assert too_few_floor_sales_reason(classify([sale(0, 1.0), sale(1, 0.5)]), 2) == "Only 1 sale at floor price in 7 days (needs 2)"
    assert too_few_floor_sales_reason(classify([sale(0, 9.0, cur="USDC")]), 1) == "Sale prices couldn't be compared with the floor"


# ---------------------------------------------------------------------------
# In a real check (floor 1.25 ETH, top offer 0.8 WETH)
# ---------------------------------------------------------------------------
def test_only_accepted_offers_rejects_with_a_clear_reason_and_stays_shortlisted():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        store.add_discovered_slugs(["solitary-voyagers"])
        ev.provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=0.81)
        assert ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True) is None
        row = store.get_results_since("2000-01-01")[0]
        det = row["details"]
        assert row["reject_filter"] == "trading_frequency"
        assert det["reason"] == "No sales at floor price in the last 7 days"
        assert (det["value"], det["limit"], det["kind"], det["unit"]) == (0, 1, "min", "floor sales in 7 days")
        assert "trading_frequency" not in det["checked"]
        fs = det["floor_sales"]
        assert (fs["count"], fs["below"], fs["total"], len(fs["rows"])) == (0, 7, 7, 7)
        assert store.count_shortlisted() == 1
        ev.provider.get_top_offer.assert_not_called()  # stopped before fetching offers


def test_floor_buys_pass_and_show_in_details():
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d)
        events = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=0.81)
        events[2].price_value = 1.22
        ev.provider.get_sale_events.return_value = events
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.is_overall_pass
        assert "1 of 7 sales at floor price" in report.criteria["trading_frequency"].notes
        assert ev.last_details["floor_sales"]["count"] == 1
        assert ev.last_details["rules"]["trading_frequency"]["unit"] == "per day"


def test_minimum_zero_or_rule_off_never_rejects_on_floor_sales():
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d, trading_frequency={"min_floor_sales_7d": 0})
        ev.provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=0.81)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.criteria["trading_frequency"].result.value == "PASS"
        assert ev.last_details["floor_sales"]["count"] == 0  # still measured and shown
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d, trading_frequency={"enabled": False})
        ev.provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=0.81)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        crit = report.criteria["trading_frequency"]
        assert crit.result.value == "OBSERVE" and "would have been FAIL" in crit.notes
        rule = ev.last_details["rules"]["trading_frequency"]
        assert (rule["value"], rule["unit"], rule["enabled"]) == (0, "floor sales in 7 days", False)


def test_inspect_shows_floor_sales_as_the_failing_part():
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d)
        ev.provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=0.81)
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=False)
        assert report is not None and not report.is_overall_pass
        assert report.criteria["trading_frequency"].notes.startswith("No sales at floor price")
        det = ev.last_details
        assert (det["rule"], det["value"], det["unit"]) == ("trading_frequency", 0, "floor sales in 7 days")


def test_settings_show_and_save_the_floor_sales_minimum(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    with patch.object(DashboardRequestHandler, "config", cfg, create=True), patch.object(DashboardRequestHandler, "evaluator", ev, create=True):
        status, s = _call("GET", "/api/settings", cfg, store, ev, cfg_path)
        tf = s["rules"]["trading_frequency"]
        assert (tf["min_floor_sales_7d"], tf["floor_sale_min_pct"], tf["floor_sale_max_pct"]) == (1, 90.0, 115.0)
        status, r = _call("POST", "/api/settings", cfg, store, ev, cfg_path,
                          {"rules": {"trading_frequency": {"enabled": True, "value": 2, "min_sales_7d": 1, "min_floor_sales_7d": 2}}})
        assert status == 200 and r["success"]
        saved = yaml.safe_load((tmp_path / "overrides.yaml").read_text())
        assert saved["filters"] == {"trading_frequency": {"min_floor_sales_7d": 2}}
        assert load_config(cfg_path).filters.trading_frequency.min_floor_sales_7d == 2
        status, r = _call("POST", "/api/settings", cfg, store, ev, cfg_path,
                          {"rules": {"trading_frequency": {"min_floor_sales_7d": "Infinity"}}})
        assert status == 400


def test_without_a_snapshot_before_the_sale_allow_for_the_floor_jump():
    t = START + 2 * 86400
    after = [{"ts": t + 600, "floor_price": 1.15, "currency": "ETH"}]  # floor rose once the cheapest listing sold
    assert classify([sale(0, 1.0, ts=t)], snaps=after).rows[0].label == "floor"       # 87% of the floor after it
    assert classify([sale(0, 0.75, ts=t)], snaps=after).rows[0].label == "below"      # an accepted offer
    before = [{"ts": t - 600, "floor_price": 1.15, "currency": "ETH"}]
    assert classify([sale(0, 1.0, ts=t)], snaps=before).rows[0].label == "below"      # strict band with a real 'before'


def test_header_counts_use_one_unit_and_wash_only_weeks_say_so():
    m = classify([sale(0, 1.0, tx="sweep"), sale(1, 1.0, tx="sweep"), sale(2, 0.5)])
    assert (m.floor_sales, m.total, m.sale_rows) == (1, 2, 3)
    m = classify([sale(0, 1.0, tx="sweep"), sale(1, 1.0, tx="sweep"), sale(2, 0.5)], mode="item_quantity")
    assert (m.floor_sales, m.total) == (2, 3)
    m = classify([sale(0, 1.0, seller="0xB", buyer="0xC"), sale(1, 1.0, seller="0xC", buyer="0xB")])
    assert too_few_floor_sales_reason(m, 1) == "Only trades between the same wallets in the last 7 days"


def test_failed_sales_download_still_shows_as_waiting_not_zero_sales():
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d)
        ev.provider.get_sale_events.return_value = None
        ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=False)
        rule = ev.last_details["rules"]["trading_frequency"]
        assert rule["unit"] == "per day" and rule["value"] == "INSUFFICIENT_HISTORY"


def test_floor_moves_are_reported_before_floor_sales():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        ev.provider.get_floor_price_history.side_effect = lambda *a, **k: []  # no floor from a day ago yet
        ev.provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], price=0.81)
        assert ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True) is None
        assert store.get_results_since("2000-01-01")[0]["reject_filter"] == "floor_history_1d"
