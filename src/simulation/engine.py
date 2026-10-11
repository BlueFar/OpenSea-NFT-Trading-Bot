"""
Paper trading: keep the best collection offer on a few collections, and sell what fills just under the floor.

Nothing is placed or bought on OpenSea. At each check the simulator reads the real best offer, floor and
sales for each collection, then:

- Bidding: moves its virtual offer to one small step above the best real offer, as long as the floor stays at
  least min_profit_gap_pct above it (royalty on the resale included, the bot's "Spread over top offer" rule).
  Above that limit it holds at the limit. A real sale at or below its offer means a seller took a worse price
  than ours, so ours would have been taken: the offer fills at our price.
- Holding: lists the item the trade model's discount under the floor (never below break-even) and follows the
  floor. A real buyer paying at least our price for a floor item would have bought ours: it sells at our price.
"""
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.chains import currency_groups, gas_estimate, same_currency
from ..models.collection import SaleEvent
from ..providers.opensea.client import OpenSeaNetworkError
from ..trade_model.calculator import extract_fees_from_collection
from ..utils.logging import setup_logger
from .store import ACTIVE_STATUSES, SimStore

logger = setup_logger("simulation")

# A real sale at 90-100% of our offer hit the offer book at our level, whatever kind of order it was.
# Further below, it counts only if OpenSea's order shows a seller accepted an offer (else: a private sale or transfer).
NEAR_OFFER_PCT = 90.0
# With no other offer to beat, the first offer goes in at this share of the floor (never above the limit)
OPENING_BID_PCT = 50.0
# OpenSea can report a sale a few minutes late, so each check looks back this far (each sale is judged once)
SALE_LOOKBACK_SECONDS = 1800
# At most this many "was it an accepted offer?" OpenSea look-ups per collection per check
ORDER_LOOKUPS_PER_CHECK = 5
# Prices are rounded to this many significant digits (offers up, limits down)
PRICE_DIGITS = 4


@dataclass
class Costs:
    fee_pct: float            # OpenSea fee on the sale, %
    royalty_pct: float        # creator royalty on the sale, %
    gas: float                # per round trip, in the item's currency
    exit_discount_pct: float  # listing price under the floor, %

    @property
    def sell_cut(self) -> float:
        return (self.fee_pct + self.royalty_pct) / 100.0


# ----------------------------------------------------------------------
# Pricing rules (pure functions, unit-tested)
# ----------------------------------------------------------------------
def _round_sig(x: float, up: bool, digits: int = PRICE_DIGITS) -> float:
    if x is None or x <= 0:
        return x
    exp = math.floor(math.log10(x)) - digits + 1
    q = 10.0 ** exp
    units = round(x / q, 6)
    units = math.ceil(units) if up else math.floor(units)
    return round(units * q, max(0, -exp))


def round_up(x: float) -> float:
    return _round_sig(x, up=True)


def round_down(x: float) -> float:
    return _round_sig(x, up=False)


def max_bid_for_gap(floor: float, costs: Costs, min_gap_pct: float) -> float:
    """
    Highest offer that keeps the floor at least min_gap_pct above (offer + royalty owed on the resale).
    The bot's offer_to_floor rule with formula floor_premium_over_effective_offer, solved for the offer.
    """
    exit_price = floor * (1 - costs.exit_discount_pct / 100.0)
    return floor / (1 + min_gap_pct / 100.0) - exit_price * costs.royalty_pct / 100.0


def gap_pct(floor: Optional[float], bid: Optional[float], costs: Costs) -> Optional[float]:
    """How far the floor sits above our offer plus the royalty on the resale, in %."""
    if not floor or not bid or bid <= 0:
        return None
    effective = bid + floor * (1 - costs.exit_discount_pct / 100.0) * costs.royalty_pct / 100.0
    return (floor - effective) / effective * 100.0


def outbid_price(top: float, step_pct: float) -> float:
    """The smallest offer that beats `top` by step_pct (rounded up, so always above it)."""
    return round_up(top * (1 + step_pct / 100.0))


def decide_bid(top: Optional[float], floor: Optional[float], costs: Costs, min_gap_pct: float,
               step_pct: float, prev_bid: Optional[float]) -> Tuple[Optional[float], str, Optional[float]]:
    """
    Our offer for this check: (offer, state, limit). States:
    top      = we hold the best offer
    capped   = the best real offer is above our limit, so we hold at the limit
    alone    = no other offer to beat
    no_floor = no floor price this check, so the offer stays as it was
    paused   = even a tiny offer would break the limit (no offer)
    """
    if not floor or floor <= 0:
        return prev_bid, "no_floor", None
    limit = max_bid_for_gap(floor, costs, min_gap_pct)
    if limit <= 0:
        return None, "paused", limit
    capped_bid = round_down(limit)
    if top is None or top <= 0:
        bid = prev_bid if prev_bid else round_up(floor * OPENING_BID_PCT / 100.0)
        return min(bid, capped_bid), "alone", limit
    want = outbid_price(top, step_pct)
    if want <= limit:
        return want, "top", limit
    return capped_bid, ("top" if capped_bid > top else "capped"), limit


def break_even_ask(entry: float, costs: Costs) -> float:
    return (entry + costs.gas) / max(1e-9, 1 - costs.sell_cut)


def target_ask(floor: Optional[float], entry: float, costs: Costs, prev_ask: Optional[float]) -> Optional[float]:
    """List the discount under the floor, but never below break-even. No floor: keep the listing as it is."""
    be = break_even_ask(entry, costs)
    if floor and floor > 0:
        return round_up(max(be, floor * (1 - costs.exit_discount_pct / 100.0)))
    return prev_ask or round_up(be)


def trade_result(entry: float, exit_price: float, costs: Costs) -> Dict[str, float]:
    fees = exit_price * costs.sell_cut
    net = exit_price - fees - costs.gas - entry
    return {"fees": fees, "gas": costs.gas, "net": net, "roi_pct": net / entry * 100.0 if entry > 0 else 0.0}


def per_item_price(ev: SaleEvent) -> Optional[float]:
    if ev.price_value is None:
        return None
    return ev.price_value / max(1, ev.quantity or 1)


def in_force(ticks: List[Dict[str, Any]], ts: int) -> Optional[Dict[str, Any]]:
    """The check whose prices stood at time ts (the latest one at or before it)."""
    found = None
    for t in ticks:
        if t["ts"] > ts:
            break
        found = t
    return found


def fmt_price(x: Optional[float], cur: Optional[str]) -> str:
    if x is None:
        return "–"
    return f"{x:.{PRICE_DIGITS}g} {cur or ''}".strip()


def fmt_duration(seconds: float) -> str:
    h = max(0.0, seconds) / 3600.0
    if h < 1:
        return f"{max(1, round(h * 60))} minutes"
    if h < 48:
        return f"{h:.1f} hours"
    return f"{h / 24:.1f} days"


def describe_position(pos: Dict[str, Any], exit_discount_pct: float) -> Dict[str, Any]:
    """Adds what the dashboard shows: the spread at our offer and the profit if it fills or sells now."""
    out = dict(pos)
    if pos.get("fee_pct") is None:
        return out
    costs = Costs(pos["fee_pct"], pos.get("royalty_pct") or 0.0, pos.get("gas") or 0.0, exit_discount_pct)
    floor, bid, entry = pos.get("floor"), pos.get("bid"), pos.get("entry_price")
    if pos.get("status") in ("bidding", "stopped") and entry is None and floor and bid:
        out["gap_pct"] = gap_pct(floor, bid, costs)
        r = trade_result(bid, floor * (1 - exit_discount_pct / 100.0), costs)
        out["est_net"], out["est_roi_pct"] = r["net"], r["roi_pct"]
    if entry:
        out["break_even"] = break_even_ask(entry, costs)
        if floor:
            r = trade_result(entry, floor * (1 - exit_discount_pct / 100.0), costs)
            out["now_net"], out["now_roi_pct"] = r["net"], r["roi_pct"]
        if pos.get("ask"):
            r = trade_result(entry, pos["ask"], costs)
            out["ask_net"], out["ask_roi_pct"] = r["net"], r["roi_pct"]
    return out


# ----------------------------------------------------------------------
# The simulator
# ----------------------------------------------------------------------
class SimDataError(Exception):
    """OpenSea didn't return what a check needs; the position is left as it was."""


class PaperTrader:
    def __init__(self, provider, state_store, config, sim_store: Optional[SimStore] = None):
        self.provider = provider
        self.state_store = state_store
        self.config = config
        self.store = sim_store or SimStore(config.general.state_db_path)

    @property
    def sim(self):
        return self.config.simulation

    def seed(self) -> None:
        """Collections listed in the settings file start once; removing one on the dashboard sticks."""
        for slug in self.sim.collections or []:
            slug = (slug or "").strip()
            if slug and self.store.get_position(slug) is None:
                self.store.add_position(slug, note="Added from the settings file. The first offer goes in at the next check.")

    def tick(self, now: Optional[int] = None, keep_running: Callable[[], bool] = lambda: True) -> int:
        """One check of every active position. OpenSeaNetworkError is passed up (the bot pauses)."""
        self.seed()
        done = 0
        for pos in self.store.get_positions():
            if pos["status"] not in ACTIVE_STATUSES:
                continue
            if not keep_running():
                break
            try:
                self._step(pos, int(now if now is not None else time.time()))
                done += 1
            except OpenSeaNetworkError:
                raise
            except SimDataError as e:
                self._note_problem(pos, str(e))
            except Exception as e:  # one collection's problem must not stop the others or the bot
                logger.exception("Simulation check failed for %s", pos["slug"])
                self._note_problem(pos, f"Check failed: {e}")
        return done

    def _note_problem(self, pos: Dict[str, Any], message: str) -> None:
        if pos.get("note") == message:
            return
        p = dict(pos)
        p["note"] = message
        self.store.save_round(p, pos["rev"] or 0, events=[(int(time.time()), pos["slug"], "problem", message)])

    # ------------------------------------------------------------------
    def _load_costs(self, p: Dict[str, Any], currency: str) -> None:
        """Name, chain, fees and gas, read at the start of each round."""
        slug = p["slug"]
        coll = self.provider.get_collection(slug)
        if coll is None:
            raise SimDataError("Couldn't load the collection from OpenSea. Trying again at the next check.")
        tm = self.config.trade_model
        chain = (coll.contracts[0].chain if coll.contracts and coll.contracts[0].chain else None) \
            or self.state_store.get_collection_chain(slug) or "ethereum"
        fee, royalty, reliable = extract_fees_from_collection(coll)
        if not reliable:
            fee = tm.marketplace_fee_pct if tm.marketplace_fee_pct is not None else 1.0
        p.update({
            "name": coll.name or slug,
            "image": (coll.raw_data or {}).get("image_url") or p.get("image"),
            "chain": chain,
            "currency": currency,
            "fee_pct": float(fee or 0.0),
            "royalty_pct": float(royalty or 0.0),
            "gas": gas_estimate(chain, currency, overrides=tm.chain_gas, ethereum_default=tm.gas_estimate_eth),
        })

    def _order_kind(self, ev: SaleEvent, budget: List[int]) -> Optional[str]:
        """'offer' or 'listing' from OpenSea's order for the sale (saved answers first). None = unknown."""
        h = ev.order_hash
        if not h:
            return None
        cached = self.state_store.get_sale_orders([h]).get(h)
        if cached and cached.get("kind") in ("offer", "listing"):
            return cached["kind"]
        get_order = getattr(self.provider, "get_order_info", None)
        if get_order is None or not ev.protocol_address or not ev.chain or budget[0] <= 0:
            return None
        budget[0] -= 1
        info = get_order(ev.chain, ev.protocol_address, h)
        info = info if isinstance(info, dict) else None
        kind = (info or {}).get("kind") or "unknown"
        self.state_store.set_sale_order(h, kind, (info or {}).get("offer_type"))
        return kind if kind in ("offer", "listing") else None

    def _step(self, pos: Dict[str, Any], now: int) -> None:
        slug, rev = pos["slug"], pos["rev"] or 0
        p = dict(pos)
        events: List[tuple] = []
        ticks: List[tuple] = []
        trades: List[Dict[str, Any]] = []
        seen: List[tuple] = []
        tm, sim = self.config.trade_model, self.sim

        def say(kind: str, message: str) -> None:
            events.append((now, slug, kind, message))

        p["note"] = None  # only a problem is kept here; a check that gets this far has none

        stats = self.provider.get_collection_stats(slug)
        floor = stats.floor_price if stats and stats.floor_price and stats.floor_price > 0 else None
        currency = p.get("currency") or (stats.floor_price_symbol if stats else None)
        if currency is None:
            raise SimDataError("No floor price from OpenSea yet. Trying again at the next check.")
        if p.get("fee_pct") is None or p.get("round_started_at") is None:
            self._load_costs(p, currency)
        cur = p["currency"]
        groups = currency_groups(p["chain"])
        costs = Costs(p["fee_pct"], p["royalty_pct"], p["gas"], tm.target_sale_discount_from_floor_pct)

        sold_now = False
        # 1. Real sales since the last check: would one of them have taken our offer, or bought our listing?
        if p.get("last_sale_check") is not None:
            sales = self.provider.get_sale_events(
                slug, after_timestamp=int(p["last_sale_check"]) - SALE_LOOKBACK_SECONDS, max_pages=3)
            if sales is not None:
                fresh = sorted((e for e in sales if e.event_id), key=lambda e: e.event_timestamp)
                already = self.store.seen_sales(slug, (e.event_id for e in fresh))
                round_ticks = self.store.get_ticks(slug, round_no=p["round"])
                budget = [ORDER_LOOKUPS_PER_CHECK]
                for ev in fresh:
                    if ev.event_id in already:
                        continue
                    seen.append((slug, ev.event_id, ev.event_timestamp))
                    already.add(ev.event_id)
                    price = per_item_price(ev)
                    if price is None or not same_currency(ev.price_currency, cur, groups):
                        continue
                    t = in_force(round_ticks, ev.event_timestamp)
                    if p["status"] == "bidding" and t and t["phase"] == "bidding" and t["bid"]:
                        if self._fills(ev, price, t["bid"], budget):
                            p.update({"status": "holding", "entry_price": t["bid"], "entry_at": ev.event_timestamp,
                                      "entry_tx": ev.transaction, "entry_token": ev.token_id,
                                      "ask": None, "listed_at": None})
                            say("filled", f"Offer filled: bought #{ev.token_id or '?'} for {fmt_price(t['bid'], cur)}. "
                                          f"A seller sold one for {fmt_price(price, cur)}, less than our offer, "
                                          f"so ours would have been taken.")
                        elif price < t["bid"]:
                            say("ignored", f"Ignored a sale at {fmt_price(price, cur)}: far below our offer and not "
                                           f"an accepted offer (a private sale or transfer).")
                    elif p["status"] == "holding" and t and t["phase"] == "holding" and t["ask"]:
                        floor_then = t["floor"] or floor
                        max_pct = self.config.filters.trading_frequency.floor_sale_max_pct
                        if price >= t["ask"] and (not floor_then or price <= floor_then * max_pct / 100.0):
                            self._sell(p, ev, price, t["ask"], costs, trades, say)
                            round_ticks = []  # the next round has no prices yet
                            sold_now = True
                p["last_sale_check"] = now
        else:
            p["last_sale_check"] = now

        # 2. Move our offer or listing for the market as it is now
        if p["status"] == "bidding" and not sold_now:  # after a sale the next round starts at the next check
            top_offer = self.provider.get_top_offer(slug, currency_filter=lambda c: same_currency(c, cur, groups))
            top = top_offer.price_value if top_offer else None
            self._update_bid(p, top, floor, costs, now, say)
            ticks.append((slug, now, p["round"], "bidding", p["bid"], top, floor, None))
        elif p["status"] == "holding":
            prev = p.get("ask")
            ask = target_ask(floor, p["entry_price"], costs, prev)
            p["floor"] = floor if floor else p.get("floor")
            if p.get("listed_at") is None:
                p["listed_at"] = now
                be = break_even_ask(p["entry_price"], costs)
                where = (f"{tm.target_sale_discount_from_floor_pct:g}% under the {fmt_price(floor, cur)} floor"
                         if floor and ask > round_up(be) else "break-even, the floor is too low for a profit")
                say("listed", f"Listed it for {fmt_price(ask, cur)} ({where}).")
            elif prev and ask and abs(ask - prev) / prev >= 0.01:
                say("repriced", f"Floor moved to {fmt_price(floor, cur)}. Listing moved from "
                                f"{fmt_price(prev, cur)} to {fmt_price(ask, cur)}.")
            p["ask"] = ask
            ticks.append((slug, now, p["round"], "holding", None, None, floor, ask))
        p["last_tick_at"] = now

        if not self.store.save_round(p, rev, events, ticks, trades, seen):
            logger.info("Simulation: %s was changed on the dashboard during the check; skipped this round.", slug)

    def _fills(self, ev: SaleEvent, price: float, bid: float, budget: List[int]) -> bool:
        if price > bid:
            return False
        if price >= bid * NEAR_OFFER_PCT / 100.0:
            return True
        return self._order_kind(ev, budget) == "offer"

    def _sell(self, p, ev, price, ask, costs: Costs, trades, say) -> None:
        cur = p["currency"]
        r = trade_result(p["entry_price"], ask, costs)
        trades.append({
            "slug": p["slug"], "round": p["round"], "currency": cur,
            "entry_price": p["entry_price"], "entry_at": p["entry_at"], "entry_tx": p["entry_tx"],
            "entry_token": p["entry_token"], "exit_price": ask, "exit_at": ev.event_timestamp,
            "exit_tx": ev.transaction, "fees": r["fees"], "gas": r["gas"], "net": r["net"],
            "roi_pct": r["roi_pct"], "times_outbid": p.get("times_outbid") or 0,
        })
        held = fmt_duration(ev.event_timestamp - (p["entry_at"] or ev.event_timestamp))
        say("sold", f"Sold for {fmt_price(ask, cur)} after {held}: a buyer paid {fmt_price(price, cur)} for one. "
                    f"{'Profit' if r['net'] >= 0 else 'Loss'} {fmt_price(abs(r['net']), cur)} "
                    f"({r['roi_pct']:+.0f}%) after OpenSea fee, royalty and gas.")
        if self.sim.repeat:
            p.update({"status": "bidding", "round": (p["round"] or 1) + 1, "bid": None, "bid_state": None,
                      "competitor_top": None, "max_bid": None, "times_outbid": 0, "highest_bid": None,
                      "entry_price": None, "entry_at": None, "entry_tx": None, "entry_token": None,
                      "ask": None, "listed_at": None, "round_started_at": None})
        else:
            p.update({"status": "stopped", "entry_price": None, "entry_at": None, "entry_tx": None,
                      "entry_token": None, "ask": None, "listed_at": None})

    def _update_bid(self, p: Dict[str, Any], top: Optional[float], floor: Optional[float], costs: Costs,
                    now: int, say) -> None:
        cur, prev, prev_state = p["currency"], p.get("bid"), p.get("bid_state")
        gap = f"{self.sim.min_profit_gap_pct:g}%"
        bid, state, limit = decide_bid(top, floor, costs, self.sim.min_profit_gap_pct, self.sim.outbid_step_pct, prev)
        if p.get("round_started_at") is None:
            p["round_started_at"] = now
        if state == "no_floor":
            pass
        elif state == "paused":
            if prev_state != "paused":
                say("paused", f"No offer: at a {fmt_price(floor, cur)} floor any offer would leave less than a "
                              f"{gap} spread.")
        elif prev is None:
            if state == "top":
                say("placed", f"Placed an offer of {fmt_price(bid, cur)}, one step above the best offer "
                              f"({fmt_price(top, cur)}).")
            elif state == "capped":
                say("placed", f"Placed an offer of {fmt_price(bid, cur)}, our limit. The best offer "
                              f"({fmt_price(top, cur)}) is above it: the floor would be less than {gap} above.")
            elif state == "alone":
                say("placed", f"No other offers. Placed {fmt_price(bid, cur)}.")
        elif bid != prev:
            if top is not None and top >= prev:
                p["times_outbid"] = (p.get("times_outbid") or 0) + 1
            if state == "capped" and prev_state != "capped":
                say("capped", f"Outbid: someone offered {fmt_price(top, cur)}, above our limit. Holding at "
                              f"{fmt_price(bid, cur)}, the most that keeps the floor {gap} above.")
            elif state == "capped":
                if abs(bid - prev) / prev >= 0.01:
                    say("limit", f"Floor moved to {fmt_price(floor, cur)}, so our limit is now {fmt_price(bid, cur)}.")
            elif prev_state == "capped":
                say("top", f"Back on top at {fmt_price(bid, cur)} (best other offer {fmt_price(top, cur)}).")
            elif bid > prev:
                say("raised", f"Someone offered {fmt_price(top, cur)}. Raised ours to {fmt_price(bid, cur)}.")
            else:
                say("lowered", f"Best other offer is now {fmt_price(top, cur) if top else 'gone'}. "
                               f"Lowered ours to {fmt_price(bid, cur)}.")
        p.update({"bid": bid, "bid_state": state, "competitor_top": top, "max_bid": limit,
                  "floor": floor if floor else p.get("floor")})
        if bid is not None:
            p["highest_bid"] = max(bid, p.get("highest_bid") or 0.0)
