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

Two strategies run side by side on the same market data (one set of OpenSea calls): A keeps the fixed
min_profit_gap_pct limit; B starts there and lowers its limit step by step while it sits capped without a fill.
Collections where we were outbid in the last day are checked more often, quiet ones less often.
"""
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.chains import currency_groups, gas_estimate, same_currency
from ..metrics.floor_sales import _wash_keys
from ..models.collection import SaleEvent
from ..providers.opensea.client import OpenSeaNetworkError
from ..trade_model.calculator import extract_fees_from_collection
from ..utils.logging import setup_logger
from .store import ACTIVE_STATUSES, MAX_CHECK_GAP_SECONDS, SimStore

logger = setup_logger("simulation")

# A real sale at 90-100% of our offer hit the offer book at our level, whatever kind of order it was.
# Further below, it counts only if OpenSea's order shows a seller accepted an offer (else: a private sale or transfer).
NEAR_OFFER_PCT = 90.0
# With no other offer to beat, the first offer goes in at this share of the floor (never above the limit)
OPENING_BID_PCT = 50.0
# OpenSea can report a sale a few minutes late, so each check looks back this far (each sale is judged once)
SALE_LOOKBACK_SECONDS = 1800
# At most this many "was it an accepted offer?" OpenSea look-ups per collection per check (answers are saved,
# so strategy B reuses A's)
ORDER_LOOKUPS_PER_CHECK = 8
# Missed-fill counting only looks orders up while this many are left, so fills and sales always have look-ups
LOOKUPS_KEPT_FOR_FILLS = 4
# "Contested" = outbid within this long; "quiet" = holding the best offer unchallenged for this long
CONTESTED_WINDOW_SECONDS = 86400
# A check may run this much early (the bot's loop doesn't land exactly on time)
DUE_SLACK_SECONDS = 60
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


def closing_trade(pos: Dict[str, Any], exit_discount_pct: float, now: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """A bought item taken out of the simulation, valued as if sold the discount under its last floor."""
    entry, floor = pos.get("entry_price"), pos.get("floor")
    if not entry or not floor or pos.get("fee_pct") is None:
        return None
    costs = Costs(pos["fee_pct"], pos.get("royalty_pct") or 0.0, pos.get("gas") or 0.0, exit_discount_pct)
    exit_price = floor * (1 - exit_discount_pct / 100.0)
    r = trade_result(entry, exit_price, costs)
    return {"slug": pos["slug"], "round": pos.get("round"), "currency": pos.get("currency"),
            "entry_price": entry, "entry_at": pos.get("entry_at"), "entry_tx": pos.get("entry_tx"),
            "entry_token": pos.get("entry_token"), "entry_metrics": pos.get("entry_metrics"),
            "exit_price": exit_price, "exit_at": int(now or time.time()),
            "exit_tx": None, "fees": r["fees"], "gas": r["gas"], "net": r["net"], "roi_pct": r["roi_pct"],
            "times_outbid": pos.get("times_outbid") or 0, "how": "removed"}


# ----------------------------------------------------------------------
# The simulator
# ----------------------------------------------------------------------
class SimDataError(Exception):
    """OpenSea didn't return what a check needs; the position is left as it was."""


# The bot's measurements saved with each fill (see PaperTrader._bot_metrics)
BOT_METRIC_KEYS = {"spread", "listed_pct", "listed", "sales_7d", "sales_daily", "move_1d", "move_7d", "floor_1d",
                   "floor_7d", "owners", "supply", "pace", "roi", "net", "floor", "offer", "royalty_pct", "fee_pct",
                   "bid_premium_pct", "trading_frequency", "verified", "created_date", "chain"}

TRIAL_DAYS = 7  # longest a Final list pick that stopped passing gets to finish its trial round


def check_interval(pos: Dict[str, Any], sim, now: int) -> int:
    """
    How often a position is checked, in seconds: contested_interval_seconds while someone outbid us in the last
    day, quiet_interval_seconds once we've held the best offer unchallenged for a day, interval_seconds otherwise
    (and always while holding an item).
    """
    base = sim.interval_seconds
    if pos.get("status") == "bidding":
        outbid = pos.get("last_outbid_at")
        if outbid and now - outbid < CONTESTED_WINDOW_SECONDS:
            return min(base, sim.contested_interval_seconds)
        started = pos.get("round_started_at")
        if pos.get("bid_state") in ("top", "alone") and started and now - started >= CONTESTED_WINDOW_SECONDS:
            return max(base, sim.quiet_interval_seconds)
    return base


class MarketView:
    """One collection's market at one check, read once from OpenSea and shared by both strategies."""

    def __init__(self, provider, slug: str):
        self.provider, self.slug = provider, slug
        self._memo: Dict[Any, Any] = {}
        self._sales: Optional[List[SaleEvent]] = None
        self._sales_after: Optional[int] = None
        self._sales_complete = True
        self._sales_failed = False

    def _get(self, key, fetch):
        if key not in self._memo:
            self._memo[key] = fetch()
        return self._memo[key]

    def stats(self):
        return self._get("stats", lambda: self.provider.get_collection_stats(self.slug, max_age=60.0))

    def collection(self):
        return self._get("collection", lambda: self.provider.get_collection(self.slug))

    def top_offer(self, currency: str, groups):
        return self._get(("top", currency), lambda: self.provider.get_top_offer(
            self.slug, currency_filter=lambda c: same_currency(c, currency, groups)))

    def sales(self, after: int) -> Tuple[Optional[List[SaleEvent]], bool]:
        """Sales at or after `after`, and whether the download was complete. None = it failed (retry next check)."""
        if self._sales_failed:
            return None, False  # failed once this check: the whole window is retried at the next one
        if self._sales is None or self._sales_after is None or after < self._sales_after:
            fetch_all = getattr(self.provider, "get_sale_events_since", None)
            if fetch_all is not None:
                sales, complete = fetch_all(self.slug, after_timestamp=after, max_pages=10)
            else:
                sales, complete = self.provider.get_sale_events(self.slug, after_timestamp=after, max_pages=10), True
            if sales is None:
                self._sales_failed = True
                return None, False
            self._sales, self._sales_after, self._sales_complete = list(sales), after, complete
        return [e for e in self._sales if e.event_timestamp >= after], self._sales_complete


@dataclass
class Book:
    """One strategy's positions: A (fixed limit, the main results) or B (stepped-down limit)."""
    strategy: str
    store: SimStore


class PaperTrader:
    def __init__(self, provider, state_store, config, sim_store: Optional[SimStore] = None):
        self.provider = provider
        self.state_store = state_store
        self.config = config
        self.store = sim_store or SimStore(config.general.state_db_path)
        self.store_b = SimStore(self.store.db_path, strategy="B")

    @property
    def sim(self):
        return self.config.simulation

    def books(self) -> List[Book]:
        books = [Book("A", self.store)]
        if self.sim.strategy_b.enabled:
            books.append(Book("B", self.store_b))
        return books

    def gap_for(self, book: Book, p: Dict[str, Any]) -> float:
        """The profit limit this offer works to: fixed for A, B's own (stepped-down) one for B."""
        if book.strategy == "B" and p.get("limit_pct") is not None:
            return float(p["limit_pct"])
        return float(self.sim.min_profit_gap_pct)

    def interval_for(self, pos: Dict[str, Any], now: int) -> int:
        return check_interval(pos, self.sim, now)

    def tick_every(self) -> int:
        """How often the bot should run tick(only_due=True): the shortest interval any position can have."""
        sim = self.sim
        return max(60, min(sim.interval_seconds, sim.contested_interval_seconds))

    def is_due(self, pos: Dict[str, Any], now: int) -> bool:
        last = pos.get("last_tick_at")
        return last is None or now - last >= self.interval_for(pos, now) - DUE_SLACK_SECONDS

    def seed(self, now: Optional[int] = None) -> None:
        """
        Keeps the simulation in step with the settings file, the Final list and the Candidates page. Cheap (no
        OpenSea calls), so the bot runs it after every collection check, not just every simulation round.
        - Settings file and Final list collections start once; removing one on the dashboard sticks.
        - include_candidates: every collection that passes the bot's rules is simulated. One that stops passing
          is removed at once, or after its bought item sells; it comes back if it passes again.
        - A Final list pick that stops passing gets a trial: one more simulated round (TRIAL_DAYS at most, longer
          while a bought item is worth a profit at today's floor). A profit keeps it on the Final list; otherwise
          it is removed from both.
        """
        now = int(now if now is not None else time.time())
        if self.sim.final_list:
            self.store.seed_final(self.sim.final_list)
        final = {f["slug"] for f in self.store.get_final_list()}
        passing = set(self.store.passing_candidates())
        failing = set(self.store.failing_candidates())
        for book in self.books():
            self._seed_book(book, final, passing)
        self._final_trials(final, passing, failing, now)

    def _seed_book(self, book: Book, final, passing) -> None:
        store = book.store
        keep = set(final)
        for slug, source, note in (
            [(s, "config", "Added from the settings file.") for s in self.sim.collections or []]
            + [(s, "final", "Added from the Final list.") for s in sorted(final)]
        ):
            slug = (slug or "").strip()
            keep.add(slug)
            if slug and store.get_position(slug) is None:
                store.add_position(slug, note=note + " The first offer goes in at the next check.", source=source)
        if book.strategy == "B":
            # B follows A's collections, including ones added by hand; a collection stopped in A is stopped in B
            for a in self.store.get_positions():
                if store.get_position(a["slug"]) is None:
                    store.add_position(a["slug"], source=a.get("source") or "manual",
                                       note="Strategy B: the first offer goes in at the next check.")
                    if a["status"] == "stopped" and not a.get("auto_stopped"):
                        store.set_status(a["slug"], "stop", note="Stopped, like strategy A.")
        if not self.sim.include_candidates:
            return
        for slug in sorted(passing):
            pos = store.get_position(slug)
            if pos is None:
                store.add_position(slug, note="New candidate. The first offer goes in at the next check.",
                                   source="candidate")
            elif pos.get("auto_stopped") and pos["status"] in ("stopped", "removed"):
                store.set_status(slug, "start", note="Back on the Candidates list: bidding again.")
            elif pos.get("leaving"):
                store.set_fields(slug, leaving=0, event=("start", "Back on the Candidates list: it bids again "
                                                                  "after this sale."))
        for pos in store.get_positions():
            slug = pos["slug"]
            if pos.get("source") != "candidate" or slug in passing or slug in keep:
                continue
            if pos["status"] == "holding":
                if not pos.get("leaving"):
                    store.set_fields(slug, leaving=1, event=(
                        "stop", "Off the Candidates list: no new offers. It leaves the simulation once the "
                                "bought item sells."))
            elif pos["status"] == "stopped" and not pos.get("auto_stopped"):
                store.set_status(slug, "remove", note="Off the Candidates list: removed from the simulation.")
            else:
                store.auto_remove(slug, "Off the Candidates list: removed from the simulation.")

    def _final_trials(self, final, passing, failing, now: int) -> None:
        discount = self.config.trade_model.target_sale_discount_from_floor_pct
        for slug in sorted(final):
            pos = self.store.get_position(slug)
            if pos is None or pos["status"] == "removed":
                continue
            state = pos.get("trial_state")
            if slug in passing:
                if state:
                    self.store.set_fields(slug, trial_state=None, trial_round=None, trial_started_at=None, event=(
                        ("start", "Passing the bot's rules again: the trial is over and it stays on the Final list.")
                        if state == "on" else None))
                continue
            if slug not in failing or state == "kept":
                continue  # not checked by the bot yet, or already earned its place
            if state is None:
                self.store.set_fields(slug, trial_state="on", trial_round=pos.get("round") or 1, trial_started_at=now,
                                      event=("trial", f"Stopped passing the bot's rules. It stays on the Final list "
                                                      f"for one more simulated round ({TRIAL_DAYS} days at most): kept "
                                                      f"if that round makes a profit, removed everywhere if not."))
                continue
            sales = self.store.get_round_sales(slug, pos.get("trial_round") or 1)
            if sales and sales[0]["net"] is not None and sales[0]["net"] > 0:
                self.store.set_fields(slug, trial_state="kept", event=(
                    "kept", f"Trial passed: the round made {fmt_price(sales[0]['net'], sales[0]['currency'])}. "
                            f"It stays on the Final list."))
                continue
            if sales:
                why = "its round made no profit"
            elif now - (pos.get("trial_started_at") or now) >= TRIAL_DAYS * 86400:
                held = closing_trade(pos, discount, now) if pos.get("entry_price") is not None else None
                if held and held["net"] > 0:
                    continue  # a bought item worth a profit: judge the trial on its real sale, however long it takes
                why = f"no round finished within {TRIAL_DAYS} days"
            else:
                continue
            self.store.remove_final(slug)
            for book in self.books():
                bpos = pos if book.strategy == "A" else book.store.get_position(slug)
                if bpos is None or bpos["status"] == "removed":
                    continue
                closing = closing_trade(bpos, discount, now) if bpos.get("entry_price") is not None else None
                book.store.set_status(slug, "remove", closing_trade=closing,
                                      note=f"Trial failed ({why}): removed from the Final list and the simulation.")

    def tick(self, now: Optional[int] = None, keep_running: Callable[[], bool] = lambda: True,
             only_due: bool = False) -> int:
        """
        One check of every active position (only_due: just the collections whose check is due). Both strategies'
        positions on a collection are checked together from one read of its market. Returns how many collections
        were checked. OpenSeaNetworkError is passed up (the bot pauses).
        """
        now = int(now if now is not None else time.time())
        self.seed(now)
        groups: Dict[str, List[Tuple[Book, Dict[str, Any]]]] = {}
        for book in self.books():
            for pos in book.store.get_positions():
                if pos["status"] in ACTIVE_STATUSES:
                    groups.setdefault(pos["slug"], []).append((book, pos))
        done = 0
        for slug, items in groups.items():
            if not keep_running():
                break
            if only_due and not any(self.is_due(pos, now) for _, pos in items):
                continue
            market = MarketView(self.provider, slug)
            # The one with the oldest sales check first, so a single sales download covers both
            items.sort(key=lambda bp: bp[1].get("last_sale_check") or 0)
            checked = False
            for book, pos in items:
                try:
                    self._step(book, pos, now, market)
                    checked = True
                except OpenSeaNetworkError:
                    raise
                except SimDataError as e:
                    self._note_problem(book, pos, str(e), now)
                except Exception as e:  # one collection's problem must not stop the others or the bot
                    logger.exception("Simulation check failed for %s (strategy %s)", slug, book.strategy)
                    self._note_problem(book, pos, f"Check failed: {e}", now)
            done += checked
        self.seed(now)  # act on sales made this check (a leaving collection, a Final list trial) straight away
        return done

    def _note_problem(self, book: Book, pos: Dict[str, Any], message: str, now: int) -> None:
        # The attempt counts as this check, so a failing collection is retried at its normal pace
        p = dict(pos, note=message, last_tick_at=now)
        new = pos.get("note") != message
        book.store.save_round(p, pos["rev"] or 0, events=[(now, pos["slug"], "problem", message)] if new else ())

    # ------------------------------------------------------------------
    def _load_costs(self, p: Dict[str, Any], currency: str, market: MarketView) -> None:
        """Name, chain, fees and gas, read at the start of each round."""
        slug = p["slug"]
        coll = market.collection()
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

    def _bot_metrics(self, slug: str) -> Optional[str]:
        """
        What the bot's own check measured for this collection most recently (spread, listed %, sales pace,
        floor moves, ...), saved with each fill so winners and losers can later be compared on them.
        """
        try:
            r = self.state_store.get_result(slug)
        except Exception:
            return None
        if not r:
            return None
        d = r.get("details") or {}
        keep = {k: v for k, v in d.items() if k in BOT_METRIC_KEYS and isinstance(v, (int, float, str, bool))}
        keep.update(checked_on=r.get("date_str"), passed=bool(r.get("is_pass")), rule=r.get("reject_filter"))
        return json.dumps(keep, default=str)

    def _order(self, ev: SaleEvent, budget: List[int]) -> Tuple[Optional[str], Optional[str]]:
        """
        (kind, offer_type) from OpenSea's order for the sale, saved answers first. kind is "offer", "listing",
        None (OpenSea can't say) or "pending" (no look-ups left this check: judge the sale at the next one).
        """
        h = ev.order_hash
        if not h:
            return None, None
        cached = self.state_store.get_sale_orders([h]).get(h)
        if cached:
            return (cached["kind"], cached.get("offer_type")) if cached.get("kind") in ("offer", "listing") else (None, None)
        get_order = getattr(self.provider, "get_order_info", None)
        if get_order is None or not ev.protocol_address or not ev.chain:
            return None, None
        if budget[0] <= 0:
            return "pending", None
        budget[0] -= 1
        info = get_order(ev.chain, ev.protocol_address, h)
        info = info if isinstance(info, dict) else {}
        kind, offer_type = info.get("kind") or "unknown", info.get("offer_type")
        self.state_store.set_sale_order(h, kind, offer_type)
        return (kind, offer_type) if kind in ("offer", "listing") else (None, None)

    def _step(self, book: Book, pos: Dict[str, Any], now: int, market: MarketView) -> None:
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
        if pos["status"] == "bidding" and pos.get("last_tick_at"):
            # Time with an offer up since the last check (the track record's fills per week of bidding)
            p["bid_seconds"] = (p.get("bid_seconds") or 0) + min(now - pos["last_tick_at"], MAX_CHECK_GAP_SECONDS)

        stats = market.stats()  # a fresh floor, not the evaluator's
        floor = stats.floor_price if stats and stats.floor_price and stats.floor_price > 0 else None
        # The coin is fixed from the first real floor (OpenSea's stats say "ETH" when there is no floor)
        currency = p.get("currency") or (stats.floor_price_symbol if floor else None)
        if currency is None:
            raise SimDataError("No floor price from OpenSea yet. Trying again at the next check.")
        if p.get("fee_pct") is None or p.get("round_started_at") is None:
            self._load_costs(p, currency, market)
        cur = p["currency"]
        groups = currency_groups(p["chain"])
        costs = Costs(p["fee_pct"], p["royalty_pct"], p["gas"], tm.target_sale_discount_from_floor_pct)

        sold_now = False
        # 1. Real sales since the last check: would one of them have taken our offer, or bought our listing?
        if p.get("last_sale_check") is not None:
            after = int(p["last_sale_check"]) - SALE_LOOKBACK_SECONDS
            sales, complete = market.sales(after)
            if sales is not None:  # a failed download is retried whole at the next check
                if not complete:
                    say("problem", "More than 1,000 sales since the last check. Only the newest were read, so a "
                                   "fill or sale may have been missed.")
                sold_now = self._judge_sales(book, p, sales, floor, costs, groups, trades, seen, say)
                p["last_sale_check"] = now
        else:
            p["last_sale_check"] = now

        # 2. Move our offer or listing for the market as it is now
        if p["status"] == "bidding" and not sold_now:  # after a sale the next round starts at the next check
            top_offer = market.top_offer(cur, groups)
            top = top_offer.price_value if top_offer else None
            self._update_bid(book, p, top, floor, costs, now, say)
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

        if not book.store.save_round(p, rev, events, ticks, trades, seen):
            logger.info("Simulation: %s (strategy %s) was changed during the check; skipped this round.",
                        slug, book.strategy)

    def _judge_sales(self, book: Book, p, sales: List[SaleEvent], floor, costs: Costs, groups, trades, seen,
                     say) -> bool:
        """Judges each real sale once, oldest first, against the offer or listing that stood at that moment."""
        slug, cur = p["slug"], p["currency"]
        fresh = sorted((e for e in sales if e.event_id), key=lambda e: e.event_timestamp)
        already = book.store.seen_sales(slug, (e.event_id for e in fresh))
        wash = _wash_keys(fresh)  # trading with yourself is not a market
        round_ticks = book.store.get_ticks(slug, round_no=p["round"])
        budget = [ORDER_LOOKUPS_PER_CHECK]
        max_pct = self.config.filters.trading_frequency.floor_sale_max_pct
        sold = False
        for ev in fresh:
            if ev.event_id in already:
                continue
            price = per_item_price(ev)
            t = in_force(round_ticks, ev.event_timestamp)
            pending = False
            if price is None or ev.event_id in wash or t is None or not same_currency(ev.price_currency, cur, groups):
                pass
            # Only sales made while this offer stood: not before it went up, e.g. while Stopped
            elif (p["status"] == "bidding" and t["phase"] == "bidding" and t["bid"]
                  and ev.event_timestamp >= (p.get("round_started_at") or 0)):
                verdict = self._fill_verdict(ev, price, t["bid"], budget)
                pending = verdict == "pending"
                if verdict == "fill":
                    p.update({"status": "holding", "entry_price": t["bid"], "entry_at": ev.event_timestamp,
                              "entry_tx": ev.transaction, "entry_token": ev.token_id, "ask": None, "listed_at": None,
                              "entry_metrics": self._bot_metrics(slug)})
                    say("filled", f"Offer filled: bought #{ev.token_id or '?'} for {fmt_price(t['bid'], cur)}. "
                                  f"A seller sold one for {fmt_price(price, cur)}, less than our offer, "
                                  f"so ours would have been taken.")
                elif verdict == "ignored":
                    say("ignored", f"Ignored a sale at {fmt_price(price, cur)}: far below our offer, and OpenSea "
                                   f"doesn't show it as an accepted offer (likely a private sale or transfer).")
                elif verdict == "no":
                    pending = self._missed_fill(book, p, ev, price, t, floor, costs, budget, say)
            elif (p["status"] == "holding" and t["phase"] == "holding" and t["ask"]
                  and ev.event_timestamp >= (p.get("listed_at") or 0)):
                ask = t["ask"]
                # Ours is the cheapest listing only at or under the floor. Held at break-even above it, a sale
                # counts once the floor has risen to our price (the cheaper listings were bought).
                ref_floor = max(t["floor"] or 0.0, floor or 0.0)
                cheapest = not ref_floor or ask <= ref_floor
                # Far above the floor is a rare item's buyer, not a floor buyer
                if price >= ask and cheapest and (not ref_floor or price <= ref_floor * max_pct / 100.0):
                    kind, offer_type = self._order(ev, budget)
                    pending = kind == "pending"
                    # A seller accepting an offer for one particular item is not a buyer choosing a listing
                    if not pending and not (kind == "offer" and offer_type in ("trait", "item")):
                        self._sell(book, p, ev, price, ask, costs, trades, say)
                        round_ticks = []  # the next round has no prices yet
                        sold = True
            if not pending:
                seen.append((slug, ev.event_id, ev.event_timestamp))
                already.add(ev.event_id)
        return sold

    def _missed_fill(self, book: Book, p, ev: SaleEvent, price: float, t, floor, costs: Costs, budget,
                     say) -> bool:
        """
        A sale above our offer but under the floor is usually a seller accepting someone else's higher collection
        offer: a fill we missed. Counted apart: within our limit (they outbid us between checks) or above it.
        Returns True when OpenSea still has to be asked about the order (judge the sale at the next check).
        """
        ref_floor = t["floor"] or floor
        if not ref_floor or price >= ref_floor:
            return False  # at or above the floor: someone bought a listing
        cached = ev.order_hash and self.state_store.get_sale_orders([ev.order_hash]).get(ev.order_hash)
        if not cached and budget[0] <= LOOKUPS_KEPT_FOR_FILLS:
            return False  # a busy check: the count is best effort, a fill is not
        kind, offer_type = self._order(ev, budget)
        if kind == "pending":
            return True
        if kind != "offer" or offer_type != "collection":
            return False
        cur, gap = p["currency"], self.gap_for(book, p)
        limit = max_bid_for_gap(ref_floor, costs, gap)
        if price <= limit:
            p["missed_fills"] = (p.get("missed_fills") or 0) + 1
            say("missed", f"Missed a fill: a seller accepted another offer of {fmt_price(price, cur)} while ours was "
                          f"{fmt_price(t['bid'], cur)}. They had outbid us since our last check; our limit "
                          f"({fmt_price(limit, cur)}) would have covered it.")
        else:
            p["missed_above_limit"] = (p.get("missed_above_limit") or 0) + 1
            say("missed", f"Missed a fill: a seller accepted another offer of {fmt_price(price, cur)}, above our "
                          f"{gap:g}% limit ({fmt_price(limit, cur)}).")
        return False

    def _fill_verdict(self, ev: SaleEvent, price: float, bid: float, budget: List[int]) -> str:
        """fill, no, ignored (far below our offer and not an accepted offer) or pending (ask OpenSea next check)."""
        if price > bid:
            return "no"
        if price >= bid * NEAR_OFFER_PCT / 100.0:
            return "fill"
        kind, _ = self._order(ev, budget)
        if kind == "pending":
            return "pending"
        return "fill" if kind == "offer" else "ignored"

    def _sell(self, book: Book, p, ev, price, ask, costs: Costs, trades, say) -> None:
        cur = p["currency"]
        r = trade_result(p["entry_price"], ask, costs)
        trades.append({
            "slug": p["slug"], "round": p["round"], "currency": cur,
            "entry_price": p["entry_price"], "entry_at": p["entry_at"], "entry_tx": p["entry_tx"],
            "entry_token": p["entry_token"], "entry_metrics": p.get("entry_metrics"),
            "exit_price": ask, "exit_at": ev.event_timestamp,
            "exit_tx": ev.transaction, "fees": r["fees"], "gas": r["gas"], "net": r["net"],
            "roi_pct": r["roi_pct"], "times_outbid": p.get("times_outbid") or 0, "how": "sold",
        })
        held = fmt_duration(ev.event_timestamp - (p["entry_at"] or ev.event_timestamp))
        say("sold", f"Sold for {fmt_price(ask, cur)} after {held}: a buyer paid {fmt_price(price, cur)} for one. "
                    f"{'Profit' if r['net'] >= 0 else 'Loss'} {fmt_price(abs(r['net']), cur)} "
                    f"({r['roi_pct']:+.0f}%) after OpenSea fee, royalty and gas.")
        if book.strategy == "B" and p.get("limit_pct") is not None:
            # A sale: step the limit back up towards the full margin
            b = self.sim.strategy_b
            up = min(float(self.sim.min_profit_gap_pct), float(p["limit_pct"]) + b.step_pct)
            if up != p["limit_pct"]:
                say("stepup", f"Sold, so the next offer works to a {up:g}% limit (up from {p['limit_pct']:g}%).")
            p["limit_pct"] = up
        p["capped_since"] = None
        if p.get("leaving"):
            say("remove", "Off the Candidates list, so it leaves the simulation now the item has sold.")
            p.update({"status": "removed", "auto_stopped": 1, "leaving": 0, "entry_price": None, "entry_at": None,
                      "entry_tx": None, "entry_token": None, "ask": None, "listed_at": None, "entry_metrics": None})
        elif self.sim.repeat:
            p.update({"status": "bidding", "round": (p["round"] or 1) + 1, "bid": None, "bid_state": None,
                      "competitor_top": None, "max_bid": None, "times_outbid": 0, "highest_bid": None,
                      "entry_price": None, "entry_at": None, "entry_tx": None, "entry_token": None,
                      "ask": None, "listed_at": None, "round_started_at": None, "entry_metrics": None})
        else:
            p.update({"status": "stopped", "entry_price": None, "entry_at": None, "entry_tx": None,
                      "entry_token": None, "ask": None, "listed_at": None, "entry_metrics": None})

    def _update_bid(self, book: Book, p: Dict[str, Any], top: Optional[float], floor: Optional[float],
                    costs: Costs, now: int, say) -> None:
        cur, prev, prev_state = p["currency"], p.get("bid"), p.get("bid_state")
        gap_value = self.gap_for(book, p)
        bid, state, limit = decide_bid(top, floor, costs, gap_value, self.sim.outbid_step_pct, prev)
        stepped = False
        if book.strategy == "B":
            before = gap_value
            bid, state, limit, gap_value = self._step_down(p, top, floor, costs, now, bid, state, limit, gap_value,
                                                           say)
            stepped = gap_value != before
        gap = f"{gap_value:g}%"
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
            # Outbid = someone reached our offer when they were below it at the last check (not the floor moving)
            prev_top = p.get("competitor_top")
            if top is not None and top >= prev and (prev_top is None or prev_top < prev):
                p["times_outbid"] = (p.get("times_outbid") or 0) + 1
                p["last_outbid_at"] = now
            if state == "capped" and prev_state != "capped":
                say("capped", f"Outbid: someone offered {fmt_price(top, cur)}, above our limit. Holding at "
                              f"{fmt_price(bid, cur)}, the most that keeps the floor {gap} above.")
            elif state == "capped":
                if abs(bid - prev) / prev >= 0.01 and not stepped:  # a step-down already said why
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

    def _step_down(self, p: Dict[str, Any], top, floor, costs: Costs, now: int, bid, state, limit, gap_value: float,
                   say):
        """
        Strategy B: after every_hours held at its limit (capped) with no fill, lower the limit by step_pct, never
        below lowest_gap_pct. A check where we hold the best offer again restarts the clock.
        """
        b = self.sim.strategy_b
        if state != "capped":
            if state != "no_floor":
                p["capped_since"] = None
            return bid, state, limit, gap_value
        if p.get("capped_since") is None:
            p["capped_since"] = now
            return bid, state, limit, gap_value
        lowest = min(float(b.lowest_gap_pct), float(self.sim.min_profit_gap_pct))
        if now - p["capped_since"] < b.every_hours * 3600 or gap_value <= lowest:
            return bid, state, limit, gap_value
        new_gap = max(lowest, gap_value - b.step_pct)
        p["limit_pct"], p["capped_since"] = new_gap, now
        bid, state, limit = decide_bid(top, floor, costs, new_gap, self.sim.outbid_step_pct, p.get("bid"))
        say("stepdown", f"Held at our limit for {b.every_hours:g} hours with no fill, so the limit drops from "
                        f"{gap_value:g}% to {new_gap:g}%: we can now offer up to "
                        f"{fmt_price(round_down(limit) if limit else None, p['currency'])}.")
        if state != "capped":
            p["capped_since"] = None
        return bid, state, limit, new_gap
