"""
How busy each blockchain is on OpenSea, for the dashboard's "Most active chains" table.

OpenSea has no total per chain, so the bot adds up each chain's biggest collections: it asks for the top few
collections by 24h volume on each chain, then reads each one's stats (24h and 7-day volume and sales). The totals
are a lower bound, but chains compare fairly. Amounts stay in the chain's coin; the dashboard turns them into dollars.

DefiLlama's free NFT volume (all marketplaces, a few chains) is saved alongside as a cross-check.
"""
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

from ..utils.logging import setup_logger

logger = setup_logger("chain_volume")

TOP_PER_CHAIN = 5
INTERVALS = {"one_day": "day", "seven_days": "week", "seven_day": "week"}
DEFILLAMA_URL = ("https://api.llama.fi/overview/nft-volume"
                 "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
# DefiLlama's chain names (name, displayName or module, lowercased) for the OpenSea chains it covers
DEFILLAMA_CHAINS = {"ethereum": "ethereum", "polygon": "polygon", "op mainnet": "optimism", "optimism": "optimism",
                    "avalanche": "avalanche", "flow": "flow", "ronin": "ronin", "robinhood chain": "robinhood",
                    "robinhood": "robinhood", "solana": "solana", "base": "base", "arbitrum": "arbitrum",
                    "apechain": "ape_chain", "abstract": "abstract"}


def top_slugs(data: Optional[Dict[str, Any]]) -> List[str]:
    """Slugs from a /collections/top answer, in OpenSea's order."""
    out = []
    for c in (data or {}).get("collections") or []:
        if not isinstance(c, dict):
            continue
        slug = c.get("collection") or c.get("slug")
        if isinstance(slug, dict):
            slug = slug.get("collection") or slug.get("slug")
        if isinstance(slug, str) and slug and slug not in out:
            out.append(slug)
    return out


def interval_stats(data: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{"day"/"week": {"volume", "sales", "symbol"}} from a collection's stats."""
    out: Dict[str, Dict[str, Any]] = {}
    for it in (data or {}).get("intervals") or []:
        key = INTERVALS.get(str(it.get("interval") or ""))
        if not key or key in out:
            continue
        try:
            vol = float(it.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        out[key] = {"volume": vol, "sales": int(it.get("sales") or 0), "symbol": (it.get("volume_symbol") or "").strip()}
    return out


def collect_chain(client, chain: str, limit: int = TOP_PER_CHAIN) -> Optional[Dict[str, Any]]:
    """One chain's top collections with their 24h and 7-day volume. None when OpenSea gave no answer."""
    top = client.get("/api/v2/collections/top", {"chains": chain, "sort_by": "one_day_volume", "limit": limit})
    if top is None:
        return None
    cols, failed = [], 0
    for slug in top_slugs(top)[:limit]:
        stats = client.get(f"/api/v2/collections/{slug}/stats")
        iv = interval_stats(stats)
        if not iv:
            failed += 1
            continue
        cols.append({"slug": slug, **iv})
    return {"chain": chain, "collections": cols, "failed": failed, "at": time.time()}


def fetch_defillama(timeout: float = 20.0) -> Optional[Dict[str, Dict[str, Any]]]:
    """{chain id: {"day", "week", "change_1d"}} in dollars, all NFT marketplaces. None if it can't be reached."""
    try:
        r = requests.get(DEFILLAMA_URL, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:  # free site, best effort
        logger.info("DefiLlama NFT volume not available (%s).", str(e)[:100])
        return None
    out: Dict[str, Dict[str, Any]] = {}
    for p in data.get("protocols") or []:
        cid = next((DEFILLAMA_CHAINS[k] for k in (str(p.get(f) or "").strip().lower()
                                                  for f in ("name", "displayName", "module")) if k in DEFILLAMA_CHAINS), None)
        if not cid or cid in out:
            continue
        out[cid] = {"day": _num(p.get("total24h")), "week": _num(p.get("total7d")), "change_1d": _num(p.get("change_1d"))}
    return out or None


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def refresh_chain_volume(client, state_store, chains: Iterable[str], keep_running=lambda: True,
                         llama=fetch_defillama) -> int:
    """Refreshes every chain; a chain OpenSea doesn't answer for keeps its last saved figures. Returns chains saved."""
    saved = 0
    for chain in chains:
        if not keep_running():
            break
        try:
            row = collect_chain(client, chain)
        except Exception as e:
            if type(e).__name__ in ("OpenSeaNetworkError", "OpenSeaAuthError"):
                raise
            logger.warning("Chain volume for %s failed: %s", chain, e)
            row = None
        if row is not None:
            state_store.set_chain_volume(chain, row)
            saved += 1
    got = llama() if llama else None
    if got:
        state_store.update_telemetry("defillama_nft_volume", {"at": time.time(), "chains": got})
    return saved


STALE_SECONDS = 24 * 3600


def summarize(saved: Dict[str, Dict[str, Any]], prices: Dict[str, float], chains: List[Dict[str, Any]],
              llama: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """
    The dashboard's chain table: one row per chain, ranked by 24h dollar volume (chains with no dollar
    figure go last). chains: [{"id", "name", "enabled"}]; prices: {coin: usd}.
    """
    from ..utils.prices import usd_rate
    now = now or time.time()
    llama_chains = (llama or {}).get("chains") or {}
    rows = []
    for c in chains:
        cid = c["id"]
        rec = (saved.get(cid) or {}).get("data")
        row = {"id": cid, "name": c.get("name") or cid, "enabled": bool(c.get("enabled")),
               "day_usd": None, "week_usd": None, "day_sales": None, "week_sales": None, "trend": None,
               "active": None, "collections": 0, "unpriced": 0, "native": None, "at": None, "stale": False,
               "partial": False, "llama_day_usd": (llama_chains.get(cid) or {}).get("day"),
               "llama_week_usd": (llama_chains.get(cid) or {}).get("week")}
        if rec:
            cols = rec.get("collections") or []
            row["at"] = rec.get("at")
            row["stale"] = bool(rec.get("at")) and now - rec["at"] > STALE_SECONDS
            row["partial"] = bool(rec.get("failed"))
            row["collections"] = len(cols)
            sums = {"day": 0.0, "week": 0.0}
            sales = {"day": 0, "week": 0}
            native: Dict[str, Dict[str, float]] = {}
            priced_any = False
            for col in cols:
                unpriced = False
                for w in ("day", "week"):
                    iv = col.get(w) or {}
                    vol, sym = iv.get("volume") or 0.0, iv.get("symbol") or ""
                    sales[w] += int(iv.get("sales") or 0)
                    if sym:
                        native.setdefault(sym, {"day": 0.0, "week": 0.0})[w] += vol
                    rate = usd_rate(sym, prices) if sym else None
                    if rate is None:
                        if vol:
                            unpriced = True
                        continue
                    sums[w] += vol * rate
                    priced_any = True
                row["unpriced"] += int(unpriced)
            row["active"] = sum(1 for col in cols if ((col.get("day") or {}).get("sales") or 0) > 0)
            row["day_sales"], row["week_sales"] = sales["day"], sales["week"]
            if priced_any or not native:
                row["day_usd"], row["week_usd"] = sums["day"], sums["week"]
            if len(native) == 1:
                sym, v = next(iter(native.items()))
                row["native"] = {"symbol": sym, "day": v["day"], "week": v["week"]}
            if row["week_usd"]:
                row["trend"] = (row["day_usd"] or 0.0) / (row["week_usd"] / 7.0) * 100.0
        rows.append(row)
    ranked = sorted(rows, key=lambda r: (r["day_usd"] is None, -(r["day_usd"] or 0.0), -(r["week_usd"] or 0.0), r["name"]))
    rank = 0
    for r in ranked:
        if r["day_usd"] is not None and r["at"] is not None and not r["stale"]:
            rank += 1
            r["rank"] = rank
        else:
            r["rank"] = None
    times = [r["at"] for r in ranked if r["at"]]
    return {"chains": ranked, "updated_at": max(times) if times else None, "per_chain": TOP_PER_CHAIN,
            "llama_at": (llama or {}).get("at")}
