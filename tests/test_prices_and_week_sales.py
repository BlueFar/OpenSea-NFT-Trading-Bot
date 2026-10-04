"""Dollar prices for the dashboard and the trading pace's 'at least 1 sale this week' check."""
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import yaml

from src.config.settings import load_config
from src.utils import prices as P
from tests.mock_data import make_sample_sales_events
from tests.test_mac_runtime_and_chains import _call, _evaluator, _settings_env


def test_payment_tokens_give_prices_for_the_coin_not_the_wrapper():
    tokens = [
        {"symbol": "ETH", "usd_price": "2500.5"},
        {"symbol": "WETH", "usd_price": "2499"},
        {"symbol": "USDC", "usd_price": "1.0001"},
        {"symbol": "APE", "usd_price": None},
        "junk",
    ]
    got = P.prices_from_payment_tokens(tokens)
    assert set(got) == {"ETH"} and got["ETH"] in (2500.5, 2499.0)
    assert P.usd_rate("weth", {"ETH": 2500.0}) == 2500.0
    assert P.usd_rate("WAPE", {"APE": 0.5}) == 0.5
    assert P.usd_rate("USDC", {}) == 1.0
    assert P.usd_rate("RON", {}) is None


def test_price_book_prefers_fresh_opensea_and_fills_gaps_from_coingecko(tmp_path):
    from src.storage.state_store import StateStore
    store = StateStore(str(tmp_path / "bot.db"))
    store.set_usd_prices({"ETH": 2600.0, "APE": 0.4})
    calls = []

    def fake_cg(symbols):
        calls.append(sorted(symbols))
        return {"ETH": 9999.0, "RON": 0.3}

    book = P.PriceBook(store, fetcher=fake_cg)
    out = book.prices()
    assert out["ETH"] == {"usd": 2600.0, "source": "OpenSea", "updated_at": out["ETH"]["updated_at"]}
    assert out["RON"]["usd"] == 0.3 and out["RON"]["source"] == "CoinGecko"
    assert out["USDC"]["usd"] == 1.0
    book.prices()
    assert len(calls) == 1  # cached

    # An old OpenSea price gives way to CoinGecko, but is kept when CoinGecko has nothing
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    with store._get_connection() as conn:
        conn.execute("UPDATE usd_prices SET updated_at=?", (old,))
    out = P.PriceBook(store, fetcher=fake_cg).prices()
    assert out["ETH"]["usd"] == 9999.0 and out["ETH"]["source"] == "CoinGecko"
    assert out["APE"]["usd"] == 0.4 and out["APE"]["source"] == "OpenSea"


def test_candidate_details_keep_the_dollar_rate_when_found():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        ev.provider.get_collection.return_value.raw_data = {
            "payment_tokens": [{"symbol": "ETH", "usd_price": "3000"}, {"symbol": "WETH", "usd_price": "3000"}]}
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.is_overall_pass
        assert ev.last_details["usd_rate"] == 3000.0
        assert ev.last_details["offer_usd_rate"] == 3000.0
        assert store.get_usd_prices()["ETH"]["usd"] == 3000.0


def test_no_sales_this_week_rejects_early_with_a_clear_reason():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        ev.provider.get_sale_events.return_value = make_sample_sales_events([0, 0, 0, 0, 0, 0, 0])
        assert ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True) is None
        row = store.get_results_since("2000-01-01")[0]
        assert row["reject_filter"] == "trading_frequency"
        det = row["details"]
        assert det["reason"] == "No sales in the last 7 days"
        assert (det["limit"], det["kind"], det["unit"]) == (1, "min", "sales in 7 days")


def test_week_minimum_off_or_rule_off_lets_quiet_collections_through():
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d, trading_frequency={"min_sales_7d": 0})
        ev.provider.get_sale_events.return_value = make_sample_sales_events([0, 0, 0, 0, 0, 0, 0])
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.criteria["trading_frequency"].result.value == "PASS"
    with tempfile.TemporaryDirectory() as d:
        ev, _ = _evaluator(d, trading_frequency={"enabled": False})
        ev.provider.get_sale_events.return_value = make_sample_sales_events([0, 0, 0, 0, 0, 0, 0])
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.criteria["trading_frequency"].result.value == "OBSERVE"


def test_settings_show_and_save_the_week_minimum(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    with patch.object(DashboardRequestHandler, "config", cfg, create=True), patch.object(DashboardRequestHandler, "evaluator", ev, create=True):
        status, s = _call("GET", "/api/settings", cfg, store, ev, cfg_path)
        assert s["rules"]["trading_frequency"]["min_sales_7d"] == 1
        status, r = _call("POST", "/api/settings", cfg, store, ev, cfg_path,
                          {"rules": {"trading_frequency": {"enabled": True, "value": 2, "min_sales_7d": 3}}})
        assert status == 200 and r["success"]
        saved = yaml.safe_load((tmp_path / "overrides.yaml").read_text())
        assert saved["filters"] == {"trading_frequency": {"min_sales_7d": 3}}
        assert load_config(cfg_path).filters.trading_frequency.min_sales_7d == 3


def test_prices_endpoint(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    store.set_usd_prices({"ETH": 2500.0})
    book = P.PriceBook(store, fetcher=lambda syms: {})
    with patch.object(DashboardRequestHandler, "price_book", book):
        status, r = _call("GET", "/api/prices", cfg, store, ev, cfg_path)
    assert status == 200
    assert r["prices"]["ETH"]["usd"] == 2500.0 and r["prices"]["USDT0"]["usd"] == 1.0
    assert r["aliases"]["WETH"] == "ETH"
