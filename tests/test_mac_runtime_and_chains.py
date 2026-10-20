"""Tests for the chain list, rule switches, power-cut/offline handling and the new dashboard endpoints."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests
import yaml

from src.collectors.orchestrator import CollectionEvaluator
from src.config.chains import ALL_CHAIN_IDS, currency_groups, gas_estimate, same_currency
from src.config.settings import BotConfig, GeneralConfig, load_config
from src.models.collection import CollectionStats, Offer
from src.providers.opensea.client import OpenSeaClient, OpenSeaNetworkError
from src.providers.opensea.provider import OpenSeaProvider
from src.runtime import control
from src.storage.state_store import StateStore
from src.trade_model.calculator import compute_trade_economics
from tests.mock_data import make_sample_floor_history, make_sample_sales_events
from tests.test_acceptance import make_realistic_passing_collection
from tests.test_dashboard import make_dummy_handler


# ---------------------------------------------------------------------------
# Chains, currencies and gas
# ---------------------------------------------------------------------------
def test_chain_list_has_27_chains_without_solana():
    assert len(ALL_CHAIN_IDS) == 27
    assert len(set(ALL_CHAIN_IDS)) == 27
    for left_out in ("solana", "hyperliquid", "b3"):
        assert left_out not in ALL_CHAIN_IDS
    for wanted in ("ethereum", "base", "abstract", "robinhood", "ape_chain", "megaeth"):
        assert wanted in ALL_CHAIN_IDS


def test_default_config_scans_every_chain():
    assert BotConfig().discovery.chains == ALL_CHAIN_IDS
    assert load_config("config/config.yaml", use_overrides=False).discovery.chains == ALL_CHAIN_IDS


def test_currency_groups_match_coin_and_wrapped_coin():
    ape = currency_groups("ape_chain")
    assert same_currency("APE", "WAPE", ape)
    assert same_currency("ETH", "WETH", ape)
    assert not same_currency("APE", "WETH", ape)
    assert not same_currency("USDG", "WETH", currency_groups("robinhood"))


def test_gas_estimate_is_in_the_items_currency():
    assert gas_estimate("ethereum", "ETH") == 0.005
    assert gas_estimate("base", "WETH") == 0.0002
    assert gas_estimate("ape_chain", "APE") == 0.05
    assert gas_estimate("polygon", "WETH") == 0.0002     # WETH-priced item, gas paid in POL
    assert gas_estimate("polygon", "POL") == 0.05
    assert gas_estimate("arc", "USDC") == 0.05
    assert gas_estimate("ape_chain", "USDC") == 0.0
    assert gas_estimate("base", "ETH", overrides={"base": 0.001}) == 0.001
    assert gas_estimate("ethereum", "ETH", ethereum_default=0.002) == 0.002


def test_ape_offer_counts_against_ape_floor():
    without = compute_trade_economics(10.0, 6.0, floor_currency="APE", top_offer_currency="WAPE",
                                      fallback_marketplace_fee_pct=1.0)
    with_groups = compute_trade_economics(10.0, 6.0, floor_currency="APE", top_offer_currency="WAPE",
                                          fallback_marketplace_fee_pct=1.0, gas_estimate_eth=0.05,
                                          currency_groups=currency_groups("ape_chain"))
    assert without.modelled.estimated_net_profit is None
    assert with_groups.modelled.estimated_net_profit is not None
    assert with_groups.modelled.floor_premium_over_effective_offer_pct > 40


# ---------------------------------------------------------------------------
# Rule switches
# ---------------------------------------------------------------------------
def _evaluator(temp_dir, listed=250, **filter_overrides):
    db_path = os.path.join(temp_dir, "bot.db")
    store = StateStore(db_path)
    raw = {"general": {"state_db_path": db_path, "data_root": temp_dir}, "filters": filter_overrides}
    cfg = BotConfig(**raw)
    provider = MagicMock()
    provider.get_collection.return_value = make_realistic_passing_collection()
    provider.get_active_listings_count.return_value = (listed, False)
    provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1], offer_price=0.5)
    curr_floor, p1, p7 = make_sample_floor_history(1.25, 2.0, 3.0)
    provider.get_collection_stats.return_value = CollectionStats(floor_price=curr_floor)
    provider.get_floor_price_history.side_effect = lambda slug, timeframe="one_day": p1 if timeframe == "one_day" else p7
    provider.get_top_offer.return_value = Offer(order_hash="0x1", chain="ethereum", price_value=0.8, price_currency="WETH")
    return CollectionEvaluator(provider, store, cfg), store


def test_rule_switched_on_rejects_early():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d, listed=900)  # 9% listed, limit 6%
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is None
        rows = store.get_results_since("2000-01-01")
        assert rows[0]["reject_filter"] == "listed_items"
        det = rows[0]["details"]
        assert det["early_exit"] is True and det["limit"] == 6.0 and det["kind"] == "max"
        assert abs(det["value"] - 9.0) < 1e-9


def test_rule_switched_off_is_measured_but_never_rejects():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d, listed=900, listed_items={"enabled": False, "max_listed_pct": 6.0})
        report = ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=True)
        assert report is not None and report.is_overall_pass
        crit = report.criteria["listed_items"]
        assert crit.result.value == "OBSERVE"
        assert "would have been FAIL" in crit.notes
        assert not any(r.startswith("listed_items") for r in report.rejection_reasons)
        assert ev.last_details["rules"]["listed_items"]["enabled"] is False


# ---------------------------------------------------------------------------
# Offline handling and sales paging
# ---------------------------------------------------------------------------
def test_client_raises_network_error_when_offline():
    client = OpenSeaClient(api_key="x", request_delay=0, max_retries=1)
    with patch.object(client.session, "request", side_effect=requests.exceptions.ConnectionError("down")), \
         patch("time.sleep"):
        with pytest.raises(OpenSeaNetworkError):
            client.get("/api/v2/collections/foo")


def test_sales_paging_stops_once_over_the_limit():
    now = int(datetime.now(timezone.utc).timestamp())
    page = {"asset_events": [
        {"event_type": "sale", "event_timestamp": now - 3600 * (i + 1), "transaction": f"0x{i}", "quantity": 1,
         "payment": {"quantity": "1000000000000000000", "decimals": 18, "symbol": "ETH"}}
        for i in range(20)
    ], "next": "cursor"}
    client = MagicMock()
    client.get.return_value = page
    provider = OpenSeaProvider(client)
    events = provider.get_sale_events("foo", after_timestamp=now - 7 * 86400, max_pages=10, stop_above=14)
    assert client.get.call_count == 1
    assert len(events) == 20


# ---------------------------------------------------------------------------
# Restart after a power cut
# ---------------------------------------------------------------------------
@pytest.fixture
def state_dir(tmp_path):
    with patch.object(control, "DESIRED_STATE_FILE", str(tmp_path / "desired_state.json")):
        yield tmp_path


def test_desired_state_round_trip(state_dir):
    assert control.read_desired_state()["run"] is False
    control.write_desired_state(True, dry_run=True)
    s = control.read_desired_state()
    assert s["run"] is True and s["dry_run"] is True and s["updated_at"]


def test_supervisor_restarts_bot_only_when_it_should_run(state_dir):
    sup = control.Supervisor("config/config.yaml", state_store=MagicMock())
    with patch.object(control.BotProcessManager, "get_running_pid", return_value=None), \
         patch.object(control.BotProcessManager, "start_bot", return_value={"success": True, "pid": 1}) as start:
        assert sup.check_once() is None               # user turned it off: stays off
        start.assert_not_called()
        control.write_desired_state(True)
        boot = control.Supervisor("config/config.yaml", state_store=MagicMock())
        assert boot.check_once() == "started"         # first check after a power cut
        boot.state_store.log_event.assert_called_with("restart", "Bot started by itself after the iMac or the dashboard restarted.")
        assert sup.check_once() == "started"          # bot died while the dashboard was running
        sup.state_store.log_event.assert_called_with("restart", "Bot had stopped unexpectedly and was restarted.")
        assert start.call_count == 2


def test_supervisor_backs_off_after_repeated_crashes(state_dir):
    control.write_desired_state(True)
    sup = control.Supervisor("config/config.yaml")
    with patch.object(control.BotProcessManager, "get_running_pid", return_value=None), \
         patch.object(control.BotProcessManager, "start_bot", return_value={"success": True, "pid": 1}) as start:
        results = [sup.check_once() for _ in range(5)]
    assert results[:3] == ["started"] * 3
    assert results[3] == "backoff"
    assert results[4] is None
    assert start.call_count == 3


# ---------------------------------------------------------------------------
# Scheduler takes turns across chains
# ---------------------------------------------------------------------------
def test_due_collections_take_turns_across_chains():
    with tempfile.TemporaryDirectory() as d:
        store = StateStore(os.path.join(d, "bot.db"))
        meta = {f"eth-{i}": {"chain": "ethereum", "safelist_status": "verified"} for i in range(10)}
        meta.update({"ape-1": {"chain": "ape_chain", "safelist_status": "verified"},
                     "base-1": {"chain": "base", "safelist_status": "verified"},
                     "skip-1": {"chain": "base", "safelist_status": "not_requested"},
                     "off-1": {"chain": "zora", "safelist_status": "verified"}})
        store.add_discovered_slugs(list(meta), "collections", meta=meta)
        due = store.get_collections_due_for_evaluation(limit=3, chains=["ethereum", "ape_chain", "base"],
                                                       allowed_statuses=["verified"])
        assert len(due) == 3 and {"ape-1", "base-1"} <= set(due)
        assert sum(slug.startswith("eth-") for slug in due) == 1
        assert "skip-1" not in due and "off-1" not in due


# ---------------------------------------------------------------------------
# Dashboard: settings, overview and near misses
# ---------------------------------------------------------------------------
def _settings_env(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["general"]["state_db_path"] = str(tmp_path / "bot.db")
    raw["general"]["data_root"] = str(tmp_path / "data")
    cfg_path.write_text(yaml.safe_dump(raw))
    cfg = load_config(str(cfg_path))
    store = StateStore(cfg.general.state_db_path)
    evaluator = CollectionEvaluator(MagicMock(), store, cfg)
    return str(cfg_path), cfg, store, evaluator


def _call(method, path, cfg, store, evaluator, cfg_path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    h = make_dummy_handler(method, path, body=raw, config=cfg, state_store=store, evaluator=evaluator)
    h.config_path = cfg_path
    (h.do_POST if method == "POST" else h.do_GET)()
    return h._status, json.loads(h.wfile.getvalue().decode("utf-8"))


def test_settings_lists_all_chains_and_saves_only_changes(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    with patch.object(DashboardRequestHandler, "config", cfg, create=True), patch.object(DashboardRequestHandler, "evaluator", ev, create=True):
        status, s = _call("GET", "/api/settings", cfg, store, ev, cfg_path)
        assert status == 200
        assert len(s["chains"]) == 27 and all(c["enabled"] for c in s["chains"])
        assert s["rules"]["offer_to_floor"] == {"enabled": True, "value": 40.0}

        s["rules"]["listed_items"]["enabled"] = False
        s["rules"]["offer_to_floor"]["value"] = 45
        for c in s["chains"]:
            if c["id"] == "zora":
                c["enabled"] = False
            if c["id"] == "base":
                c["gas"] = 0.0005
        status, r = _call("POST", "/api/settings", cfg, store, ev, cfg_path,
                          {"rules": s["rules"], "trade": s["trade"], "chains": s["chains"],
                           "skip_unverified": s["skip_unverified"], "runtime": s["runtime"]})
        assert status == 200 and r["success"]

        saved = yaml.safe_load((tmp_path / "overrides.yaml").read_text())
        assert saved["filters"] == {"listed_items": {"enabled": False}, "offer_to_floor": {"min_ratio_pct": 45.0}}
        assert saved["trade_model"] == {"chain_gas": {"base": 0.0005}}
        assert "zora" not in saved["discovery"]["chains"] and len(saved["discovery"]["chains"]) == 26
        assert "runtime" not in saved

        new_cfg = load_config(cfg_path)
        assert new_cfg.filters.listed_items.enabled is False
        assert new_cfg.filters.offer_to_floor.min_ratio_pct == 45.0
        assert new_cfg.trade_model.chain_gas == {"base": 0.0005}

        # Switching the rule back on removes it from overrides again
        status, _ = _call("POST", "/api/settings", cfg, store, ev, cfg_path, {"rules": {"listed_items": {"enabled": True, "value": 6}}})
        saved = yaml.safe_load((tmp_path / "overrides.yaml").read_text())
        assert "listed_items" not in saved["filters"]


def test_settings_rejects_bad_values_and_keeps_old_file(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    with patch.object(DashboardRequestHandler, "config", cfg, create=True), patch.object(DashboardRequestHandler, "evaluator", ev, create=True):
        status, r = _call("POST", "/api/settings", cfg, store, ev, cfg_path, {"chains": [{"id": "solana", "enabled": True}]})
        assert status == 400 and "solana" in r["error"]
        status, r = _call("POST", "/api/settings", cfg, store, ev, cfg_path, {"rules": {"net_profit": {"value": -5}}})
        assert status == 400
        assert not (tmp_path / "overrides.yaml").exists()


def test_overview_and_near_misses(tmp_path):
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    from src.utils.time import now_local
    today = now_local(cfg.general.bot_timezone).strftime("%Y-%m-%d")  # the bot's day, not the machine's
    store.record_candidate("close", today, False, "x", reject_filter="offer_to_floor",
                           details={"name": "Close", "rule": "offer_to_floor", "value": 35.0, "limit": 40.0, "kind": "min"})
    store.record_candidate("far", today, False, "x", reject_filter="listed_items",
                           details={"name": "Far", "rule": "listed_items", "value": 30.0, "limit": 6.0, "kind": "max"})
    store.record_candidate("wait", today, False, "x", reject_filter="floor_history_7d", details={"name": "Wait"})
    store.record_candidate("win", today, True, "PASS", details={"name": "Win"})
    store.log_event("candidate", "New candidate: Win.")

    with patch.object(control, "DESIRED_STATE_FILE", str(tmp_path / "desired.json")), \
         patch.object(control.BotProcessManager, "get_running_pid", return_value=None), \
         patch("src.runtime.launchd.is_installed", return_value=False):
        status, o = _call("GET", "/api/overview", cfg, store, ev, cfg_path)
    assert status == 200
    assert o["state"] == "stopped" and o["candidates_7d"] == 1 and o["checked_today"] == 4
    assert o["funnel"]["PASS"] == 1 and o["floor_history"]["ready"] is False
    assert o["events"][0]["kind"] == "candidate"

    status, n = _call("GET", "/api/near-misses", cfg, store, ev, cfg_path)
    assert [r["slug"] for r in n["near"]] == ["close"]
    assert [r["slug"] for r in n["waiting"]] == ["wait"]

    status, c = _call("GET", "/api/candidate?slug=win", cfg, store, ev, cfg_path)
    assert status == 200 and c["is_pass"] and c["info_md_content"] is None


def test_candidate_endpoint_never_reads_files_outside_data_folder(tmp_path):
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    secret = tmp_path / "Info.md"
    secret.write_text("outside")
    store.record_candidate("x", "2026-10-01", True, "PASS", details={"info_md": str(secret)})
    status, c = _call("GET", "/api/candidate?slug=x", cfg, store, ev, cfg_path)
    assert status == 200 and c["info_md_content"] is None


def test_stop_request_cuts_retry_wait_short():
    import time as _time
    client = OpenSeaClient(api_key="x", request_delay=0, max_retries=5, backoff_factor=10)
    client.stop_event.set()
    t = _time.time()
    with patch.object(client.session, "request", side_effect=requests.exceptions.ConnectionError("down")):
        with pytest.raises(OpenSeaNetworkError):
            client.get("/api/v2/collections/foo")
    assert _time.time() - t < 1.0


def test_rate_limit_wait_cancelled_raises_instead_of_returning_none():
    client = OpenSeaClient(api_key="x", request_delay=0, max_retries=2)
    resp = MagicMock(status_code=429, headers={"retry-after": "30"})
    def stop_then_429(**kwargs):
        client.stop_event.set()
        return resp
    with patch.object(client.session, "request", side_effect=stop_then_429):
        with pytest.raises(OpenSeaNetworkError):
            client.get("/api/v2/collections/foo")


# ---------------------------------------------------------------------------
# A key changed in .env is used without reinstalling
# ---------------------------------------------------------------------------
def test_reload_env_replaces_key_inherited_from_parent(tmp_path, monkeypatch):
    from src.config import settings
    env = tmp_path / ".env"
    env.write_text("OPENSEA_API_KEY=new-key\n")
    monkeypatch.setattr(settings, "ENV_PATH", str(env))
    monkeypatch.setenv("OPENSEA_API_KEY", "old-key-from-dashboard")
    assert settings.reload_env() == "new-key"


def test_running_bot_switches_to_new_key_from_env(tmp_path, monkeypatch):
    import time as _time
    from src import bot as bot_module
    from src.config import settings
    env = tmp_path / ".env"
    env.write_text("OPENSEA_API_KEY=old-key\n")
    monkeypatch.setattr(settings, "ENV_PATH", str(env))
    monkeypatch.setattr(bot_module, "ENV_PATH", str(env))
    monkeypatch.delenv("OPENSEA_API_KEY", raising=False)
    cfg_path, _, _, _ = _settings_env(tmp_path)

    b = bot_module.NFTBot(config=load_config(cfg_path), config_path=cfg_path)
    try:
        assert b.client.api_key == "old-key"
        env.write_text("OPENSEA_API_KEY=new-key\n")
        os.utime(env, (_time.time() + 5, _time.time() + 5))
        b._maybe_reload_config()
        assert b.client.api_key == "new-key"
        assert b.client.session.headers["x-api-key"] == "new-key"
    finally:
        b.close()


# ---------------------------------------------------------------------------
# Broken home IPv6: connect over IPv4 only
# ---------------------------------------------------------------------------
def test_ipv4_only_switch_for_requests():
    import socket
    import urllib3.util.connection as uc
    from src.utils.net import use_ipv4_only
    try:
        use_ipv4_only(True)
        assert uc.allowed_gai_family() == socket.AF_INET
    finally:
        use_ipv4_only(False)
    assert uc.allowed_gai_family() != socket.AF_INET or not uc.HAS_IPV6


def test_online_check_only_resolves_ipv4():
    import socket
    from src.utils import net
    with patch.object(net.socket, "getaddrinfo", return_value=[]) as gai:
        assert control.is_online("api.opensea.io") is False
    assert gai.call_args[0][2] == socket.AF_INET
    with patch.object(net.socket, "getaddrinfo", return_value=[]) as gai:
        control.is_online("api.opensea.io", ipv4_only=False)
    assert gai.call_args[0][2] == socket.AF_UNSPEC
