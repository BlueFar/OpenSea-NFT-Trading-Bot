"""
OpenSea NFT chains the bot can scan, with the per-chain facts the trade model needs.

Source: OpenSea's API ChainIdentifier enum (@opensea/api-types 0.15.1, 2026-09-30) and the
offer/listing payment tokens in OpenSea's SDK (@opensea/sdk 12.11.2). Left out on purpose:
- solana: not Seaport; listing/offer parsing and the profit model need Solana-specific work
- hyperliquid: in the API enum, but it is the tokens/perps identifier, not an NFT chain
- b3: removed from OpenSea's API on 2026-09-30

Gas defaults are rough per-trade estimates in the chain's own coin, meant to be edited in
the dashboard's Settings page (stored as trade_model.chain_gas).
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Set


@dataclass(frozen=True)
class ChainInfo:
    id: str             # exact OpenSea API identifier
    name: str           # display name
    native: str         # native coin symbol (pays gas)
    wrapped: str        # wrapped native coin used for offers
    kind: str           # "eth" (ETH-priced), "native" (own coin) or "stable" (stablecoin-priced)
    gas: float          # default gas per trade, in the native coin


CHAINS: List[ChainInfo] = [
    # Priced in ETH
    ChainInfo("ethereum", "Ethereum", "ETH", "WETH", "eth", 0.005),
    ChainInfo("base", "Base", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("polygon", "Polygon", "POL", "WPOL", "eth", 0.05),  # items priced in WETH, gas in POL
    ChainInfo("arbitrum", "Arbitrum", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("optimism", "Optimism", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("zora", "Zora", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("blast", "Blast", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("abstract", "Abstract", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("robinhood", "Robinhood Chain", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("shape", "Shape", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("unichain", "Unichain", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("soneium", "Soneium", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("ink", "Ink", "ETH", "WETH", "eth", 0.0002),
    ChainInfo("megaeth", "MegaETH", "ETH", "WETH", "eth", 0.0002),
    # Priced in the chain's own coin
    ChainInfo("ape_chain", "ApeChain", "APE", "WAPE", "native", 0.05),
    ChainInfo("ronin", "Ronin", "RON", "WRON", "native", 0.05),
    ChainInfo("avalanche", "Avalanche", "AVAX", "WAVAX", "native", 0.01),
    ChainInfo("sei", "Sei", "SEI", "WSEI", "native", 0.1),
    ChainInfo("bera_chain", "Berachain", "BERA", "WBERA", "native", 0.01),
    ChainInfo("flow", "Flow", "FLOW", "WFLOW", "native", 0.05),
    ChainInfo("gunzilla", "GUNZ", "GUN", "WGUN", "native", 0.5),
    ChainInfo("hyperevm", "HyperEVM", "HYPE", "WHYPE", "native", 0.002),
    ChainInfo("somnia", "Somnia", "SOMI", "WSOMI", "native", 0.1),
    ChainInfo("monad", "Monad", "MON", "WMON", "native", 0.1),
    ChainInfo("animechain", "AnimeChain", "ANIME", "WANIME", "native", 5.0),
    # Priced in stablecoins
    ChainInfo("arc", "Arc", "USDC", "USDC", "stable", 0.05),
    ChainInfo("stablechain", "Stable Chain", "USDT0", "USDT0", "stable", 0.05),
]

CHAINS_BY_ID: Dict[str, ChainInfo] = {c.id: c for c in CHAINS}
ALL_CHAIN_IDS: List[str] = [c.id for c in CHAINS]

ETH_GROUP: Set[str] = {"ETH", "WETH"}
# Gas for an ETH-priced item on a chain whose own coin is not ETH (e.g. a WETH-priced Polygon item)
DEFAULT_L2_GAS_ETH = 0.0002


def get_chain(chain_id: Optional[str]) -> Optional[ChainInfo]:
    return CHAINS_BY_ID.get((chain_id or "").lower())


def currency_groups(chain_id: Optional[str]) -> List[Set[str]]:
    """Sets of currency symbols that are 1:1 interchangeable on this chain (a coin and its wrapped form)."""
    groups = [set(ETH_GROUP)]
    info = get_chain(chain_id)
    if info and info.native not in ETH_GROUP:
        groups.append({info.native, info.wrapped})
    return groups


def same_currency(a: Optional[str], b: Optional[str], groups: List[Set[str]]) -> bool:
    if not a or not b:
        return False
    a, b = a.upper(), b.upper()
    return a == b or any(a in g and b in g for g in groups)


def gas_estimate(chain_id: Optional[str], price_currency: Optional[str],
                 overrides: Optional[Dict[str, float]] = None,
                 ethereum_default: float = 0.005) -> float:
    """
    Gas per trade expressed in the currency the item is priced in.
    Uses the chain's gas (native coin) when the item is priced in that coin, otherwise a
    small ETH-L2 default for ETH-priced items, and 0 for anything else (e.g. stablecoins).
    """
    info = get_chain(chain_id) or CHAINS_BY_ID["ethereum"]
    overrides = overrides or {}
    chain_gas = overrides.get(info.id, ethereum_default if info.id == "ethereum" else info.gas)
    cur = (price_currency or "").upper()
    if same_currency(cur, info.native, [{info.native, info.wrapped}]):
        return chain_gas
    if cur in ETH_GROUP:
        if info.native in ETH_GROUP:
            return chain_gas
        return DEFAULT_L2_GAS_ETH
    return 0.0
