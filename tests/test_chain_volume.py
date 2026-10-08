"""The "Most active chains" table: adding up each chain's top collections, dollars, ranks and the dashboard call."""
import time
from unittest.mock import MagicMock, patch

from src.collectors.chain_volume import (
    fetch_defillama, interval_stats, refresh_chain_volume, summarize, top_slugs,
)
from src.storage.state_store import StateStore
from tests.test_mac_runtime_and_chains import _call, _settings_env


def stats(day_vol, week_vol, sym="ETH", day_sales=3, week_sales=20):
    return {"total": {}, "intervals": [
        {"interval": "one_day", "volume": day_vol, "volume_symbol": sym, "sales": day_sales},
        {"interval": "seven_day", "volume": week_vol, "volume_symbol": sym, "sales": week_sales},
        {"interval": "thirty_day", "volume": week_vol * 4, "volume_symbol": sym, "sales": week_sales * 4}]}


class FakeClient:
    def __init__(self, tops, stat):
        self.tops, self.stat, self.calls = tops, stat, []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/api/v2/collections/top":
            return self.tops.get(params["chains"])
        return self.stat.get(path.split("/")[4])


def test_parsing_top_list_and_intervals():
    assert top_slugs({"collections": [{"collection": "a"}, {"collection": "b"}, {"collection": "a"}, "x"]}) == ["a", "b"]
    assert top_slugs(None) == []
    iv = interval_stats(stats(1.5, 10))
    assert iv == {"day": {"volume": 1.5, "sales": 3, "symbol": "ETH"}, "week": {"volume": 10.0, "sales": 20, "symbol": "ETH"}}


def test_refresh_saves_each_chain_and_keeps_old_figures_when_opensea_fails(tmp_path):
    store = StateStore(str(tmp_path / "bot.db"))
    client = FakeClient({"base": {"collections": [{"collection": "b1"}, {"collection": "b2"}]},
                         "ape_chain": {"collections": [{"collection": "a1"}]}},
                        {"b1": stats(2, 14), "b2": None, "a1": stats(1000, 7000, "APE")})
    assert refresh_chain_volume(client, store, ["base", "ape_chain", "ethereum"], llama=lambda: None) == 2
    saved = store.get_chain_volume()
    assert set(saved) == {"base", "ape_chain"}
    assert [c["slug"] for c in saved["base"]["data"]["collections"]] == ["b1"] and saved["base"]["data"]["failed"] == 1
    # Next time OpenSea doesn't answer for base: the last figures stay
    client.tops = {}
    refresh_chain_volume(client, store, ["base"], llama=lambda: {"ethereum": {"day": 5.0, "week": 30.0, "change_1d": 1.0}})
    assert store.get_chain_volume()["base"]["data"]["collections"][0]["slug"] == "b1"
    assert "ethereum" in store.get_telemetry("defillama_nft_volume")


def test_summary_ranks_by_dollars_and_flags_unpriced_stale_and_not_scanned():
    now = time.time()
    saved = {
        "base": {"data": {"at": now, "failed": 0, "collections": [
            {"slug": "b1", "day": {"volume": 2.0, "sales": 4, "symbol": "WETH"}, "week": {"volume": 7.0, "sales": 30, "symbol": "WETH"}},
            {"slug": "b2", "day": {"volume": 0.0, "sales": 0, "symbol": "ETH"}, "week": {"volume": 7.0, "sales": 9, "symbol": "ETH"}}]}},
        "ape_chain": {"data": {"at": now, "failed": 0, "collections": [
            {"slug": "a1", "day": {"volume": 10000.0, "sales": 9, "symbol": "APE"}, "week": {"volume": 70000.0, "sales": 50, "symbol": "APE"}}]}},
        "monad": {"data": {"at": now, "failed": 0, "collections": [
            {"slug": "m1", "day": {"volume": 50.0, "sales": 2, "symbol": "MON"}, "week": {"volume": 100.0, "sales": 5, "symbol": "MON"}}]}},
        "zora": {"data": {"at": now - 2 * 86400, "failed": 0, "collections": [
            {"slug": "z1", "day": {"volume": 100.0, "sales": 2, "symbol": "ETH"}, "week": {"volume": 100.0, "sales": 5, "symbol": "ETH"}}]}},
    }
    chains = [{"id": i, "name": i.title(), "enabled": i != "ape_chain"} for i in ("base", "ape_chain", "monad", "zora", "ink")]
    out = summarize(saved, {"ETH": 4000.0, "APE": 0.5}, chains, {"chains": {"base": {"day": 9.0, "week": 50.0}}}, now=now)
    by = {c["id"]: c for c in out["chains"]}
    assert by["base"]["day_usd"] == 8000.0 and by["base"]["week_usd"] == 56000.0
    assert by["base"]["active"] == 1 and by["base"]["collections"] == 2 and by["base"]["day_sales"] == 4
    assert round(by["base"]["trend"]) == 100            # 24h equals the week's daily average
    assert by["base"]["llama_day_usd"] == 9.0
    assert by["ape_chain"]["day_usd"] == 5000.0 and by["ape_chain"]["enabled"] is False
    # No dollar price for MON: no USD figure, coin amount instead, ranked last
    assert by["monad"]["day_usd"] is None and by["monad"]["native"]["symbol"] == "MON" and by["monad"]["unpriced"] == 1
    assert by["zora"]["stale"] and by["zora"]["rank"] is None
    assert by["ink"]["at"] is None and by["ink"]["rank"] is None
    assert [c["id"] for c in out["chains"] if c["rank"]] == ["base", "ape_chain"]
    assert (by["base"]["rank"], by["ape_chain"]["rank"]) == (1, 2)


def test_defillama_maps_its_chain_names():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"protocols": [
        {"name": "Ethereum", "total24h": 2727866, "total7d": 2e7, "change_1d": 4.2},
        {"name": "Robinhood Chain", "total24h": 273301, "total7d": 1e6},
        {"displayName": "OP Mainnet", "name": "op mainnet", "total24h": 18},
        {"name": "Cardano", "total24h": None}]}
    with patch("src.collectors.chain_volume.requests.get", return_value=resp):
        got = fetch_defillama()
    assert set(got) == {"ethereum", "robinhood", "optimism"} and got["ethereum"]["day"] == 2727866.0
    with patch("src.collectors.chain_volume.requests.get", side_effect=OSError("offline")):
        assert fetch_defillama() is None


def test_chains_endpoint(tmp_path):
    from src.dashboard.server import DashboardRequestHandler
    from src.utils.prices import PriceBook
    cfg_path, cfg, store, ev = _settings_env(tmp_path)
    store.set_chain_volume("base", {"at": time.time(), "failed": 0, "collections": [
        {"slug": "b1", "day": {"volume": 1.0, "sales": 2, "symbol": "ETH"}, "week": {"volume": 7.0, "sales": 9, "symbol": "ETH"}}]})
    store.set_usd_prices({"ETH": 3000.0})
    book = PriceBook(store, fetcher=lambda ids: {})
    with patch.object(DashboardRequestHandler, "config", cfg, create=True), \
            patch.object(DashboardRequestHandler, "evaluator", ev, create=True), \
            patch.object(DashboardRequestHandler, "price_book", book, create=True):
        status, r = _call("GET", "/api/chains", cfg, store, ev, cfg_path)
    assert status == 200 and len(r["chains"]) == 27
    assert r["chains"][0]["id"] == "base" and r["chains"][0]["day_usd"] == 3000.0 and r["chains"][0]["rank"] == 1
    assert r["interval_hours"] == 6.0
