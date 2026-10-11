"""
Dollar prices for the coins collections are priced in, for the dashboard's USD hovers.

OpenSea's own numbers come first: every collection lists its payment tokens with a usd_price,
and the bot saves those as it checks collections. CoinGecko's free public API fills in coins the
bot hasn't seen a price for recently. Stablecoins count as $1 and a wrapped coin (WETH, WAPE, ...)
has the same price as the coin itself.
"""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import requests

from ..config.chains import CHAINS

logger = logging.getLogger(__name__)

STABLECOINS = {"USDC", "USDT", "USDT0", "USDC.E", "USDBC", "DAI", "USDE"}
WRAPPED_TO_NATIVE = {c.wrapped: c.native for c in CHAINS if c.wrapped != c.native}
WRAPPED_TO_NATIVE.update({"WETH": "ETH", "WMATIC": "POL", "MATIC": "POL"})

# CoinGecko coin ids for each chain's own coin (stablecoins need none)
COINGECKO_IDS = {
    "ETH": "ethereum",
    "POL": "polygon-ecosystem-token",
    "APE": "apecoin",
    "RON": "ronin",
    "AVAX": "avalanche-2",
    "SEI": "sei-network",
    "BERA": "berachain-bera",
    "FLOW": "flow",
    "GUN": "gunz",
    "HYPE": "hyperliquid",
    "SOMI": "somnia",
    "MON": "monad",
    "ANIME": "animecoin",
}
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

# An OpenSea price younger than this is used as is; older ones only when CoinGecko has nothing
FRESH_SECONDS = 2 * 3600


def canonical(symbol: Optional[str]) -> str:
    s = (symbol or "").strip().upper()
    return WRAPPED_TO_NATIVE.get(s, s)


def prices_from_payment_tokens(tokens: Optional[Iterable[Dict[str, Any]]]) -> Dict[str, float]:
    """{coin: usd} from a collection's payment_tokens list (OpenSea GET /collections/{slug})."""
    out: Dict[str, float] = {}
    for t in tokens or []:
        if not isinstance(t, dict):
            continue
        sym = canonical(t.get("symbol"))
        try:
            usd = float(t.get("usd_price"))
        except (TypeError, ValueError):
            continue
        if sym and usd > 0 and sym not in STABLECOINS:
            out[sym] = usd
    return out


def usd_rate(symbol: Optional[str], prices: Dict[str, float]) -> Optional[float]:
    sym = canonical(symbol)
    if sym in STABLECOINS:
        return 1.0
    return prices.get(sym)


def fetch_coingecko(symbols: Iterable[str], timeout: float = 10.0) -> Dict[str, float]:
    ids = {COINGECKO_IDS[s]: s for s in {canonical(x) for x in symbols} if s in COINGECKO_IDS}
    if not ids:
        return {}
    try:
        r = requests.get(COINGECKO_URL, params={"ids": ",".join(sorted(ids)), "vs_currencies": "usd"},
                         timeout=timeout, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.info("CoinGecko prices unavailable: %s", e)
        return {}
    out = {}
    for cg_id, sym in ids.items():
        usd = (data.get(cg_id) or {}).get("usd")
        if isinstance(usd, (int, float)) and usd > 0:
            out[sym] = float(usd)
    return out


class PriceBook:
    """Merges the bot's saved OpenSea prices with a cached CoinGecko lookup (dashboard side)."""

    def __init__(self, state_store, fetcher=fetch_coingecko, cache_seconds: float = 600.0):
        self.state_store = state_store
        self.fetcher = fetcher
        self.cache_seconds = cache_seconds
        self._cg: Dict[str, float] = {}
        self._cg_at = 0.0
        self._lock = threading.Lock()

    def prices(self) -> Dict[str, Dict[str, Any]]:
        """{coin: {"usd", "source", "updated_at"}} for every coin the dashboard may show."""
        now = time.time()
        saved = self.state_store.get_usd_prices()
        out: Dict[str, Dict[str, Any]] = {}
        stale = []
        for sym in set(COINGECKO_IDS) | set(saved):
            row = saved.get(sym)
            if row and _age_seconds(row.get("updated_at"), now) < FRESH_SECONDS:
                out[sym] = {"usd": row["usd"], "source": "OpenSea", "updated_at": row["updated_at"]}
            else:
                stale.append(sym)
        if stale:
            cg = self._coingecko(stale, now)
            for sym in stale:
                if sym in cg:
                    out[sym] = {"usd": cg[sym], "source": "CoinGecko",
                                "updated_at": datetime.fromtimestamp(self._cg_at, timezone.utc).isoformat()}
                elif sym in saved:
                    row = saved[sym]
                    out[sym] = {"usd": row["usd"], "source": "OpenSea", "updated_at": row["updated_at"]}
        for sym in STABLECOINS:
            out[sym] = {"usd": 1.0, "source": "Stablecoin", "updated_at": None}
        return out

    def _coingecko(self, symbols, now: float) -> Dict[str, float]:
        with self._lock:
            missing = [s for s in symbols if s in COINGECKO_IDS and s not in self._cg]
            if now - self._cg_at > self.cache_seconds or missing:
                if now - self._cg_at > 60 or not self._cg:  # at most one call a minute
                    got = self.fetcher(COINGECKO_IDS.keys())
                    if got:
                        self._cg = got
                        self._cg_at = now
            return dict(self._cg)


def _age_seconds(iso: Optional[str], now: float) -> float:
    if not iso:
        return float("inf")
    try:
        return now - datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return float("inf")
