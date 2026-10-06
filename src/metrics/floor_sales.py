"""
Floor-price sales: which of last week's sales were bought at about the floor price.

A collection can trade often while every sale is someone accepting the top offer. Those sales say
nothing about whether a listing a little under the floor will sell, which is how the strategy exits.
So each sale is compared with the floor at the time it happened:
  - floor:  paid min_pct..max_pct of that floor (default 90-115%) -> counts as a floor-price sale
  - below:  paid less (usually an accepted offer)
  - above:  paid more (usually a rare item)
  - skipped: no price, a coin that can't be compared with the floor, or a likely wash trade
When OpenSea says how a floor-priced sale happened, two more labels keep a collection from slipping in:
  - offer:  a seller accepted an offer near the floor price; nobody bought a listing
  - rare:   a listing was bought, but the item is among the collection's rarest, so it says little about the rest

classify_offer_sales answers the other side of the trade: did any seller accept a collection offer (the kind
of bid the strategy places) in the last 14 days?
"""
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..config.chains import same_currency
from ..models.collection import SaleEvent
from .calculator import deduplicate_sale_events

FLOOR, BELOW, ABOVE, SKIPPED, OFFER, RARE = "floor", "below", "above", "skipped", "offer", "rare"
LISTING_BUY, OFFER_ACCEPTED = "listing", "offer"
# order_info(sale) -> {"kind": "listing" | "offer", "offer_type": "collection" | "trait" | "item" | None}, or None
# when unknown; rarity_rank(sale) -> OpenSea rarity rank (1 = rarest), or None when there is none.
OrderInfo = Callable[[SaleEvent], Optional[Dict]]
RarityRank = Callable[[SaleEvent], Optional[int]]
MAX_ROWS = 20  # sales kept for the dashboard's "What buyers paid" list


@dataclass
class SaleRow:
    ts: int
    price: Optional[float]      # per item
    currency: Optional[str]
    ref_floor: Optional[float]  # the floor this sale was compared with
    ref_source: str             # "before" / "after" (bot snapshot around the sale) or "now" (today's floor)
    pct: Optional[float]        # price as % of ref_floor
    label: str
    note: str = ""
    how: str = ""               # "listing" / "offer" when OpenSea's order says so, "" = not looked up or unknown
    rank: Optional[int] = None  # rarity rank when looked up (1 = rarest)
    event: Optional[SaleEvent] = field(default=None, repr=False, compare=False)

    def as_dict(self) -> Dict:
        return {"ts": self.ts, "price": self.price, "currency": self.currency, "ref_floor": self.ref_floor,
                "ref_source": self.ref_source, "pct": None if self.pct is None else round(self.pct, 1),
                "label": self.label, "note": self.note, "how": self.how, "rank": self.rank}


@dataclass
class FloorSalesMetrics:
    floor_sales: int = 0   # counted like the pace rule (distinct transactions, or items)
    priced: int = 0        # sales that could be compared with the floor
    below: int = 0
    above: int = 0
    skipped: int = 0
    wash: int = 0          # skipped because the same wallets traded with each other
    offers: int = 0        # priced like a floor sale, but a seller accepted an offer
    rare: int = 0          # priced like a floor sale, but the item is among the rarest
    rare_pct: float = 0.0  # "rarest" = rank within this % of the supply (0 = rarity not checked)
    total: int = 0         # sales in the window, counted the same way as floor_sales
    sale_rows: int = 0     # individual sale events in the window (one table row each)
    min_pct: float = 90.0
    max_pct: float = 115.0
    rows: List[SaleRow] = field(default_factory=list)

    def summary(self, needed: int) -> Dict:
        return {"count": self.floor_sales, "needed": needed, "priced": self.priced, "below": self.below,
                "above": self.above, "skipped": self.skipped, "wash": self.wash, "total": self.total,
                "sale_rows": self.sale_rows, "offers": self.offers, "rare": self.rare, "rare_pct": self.rare_pct,
                "min_pct": self.min_pct, "max_pct": self.max_pct,
                "rows": [r.as_dict() for r in self.rows[:MAX_ROWS]]}

    def breakdown(self) -> str:
        parts = [f"{self.floor_sales} of {self.total} sales at floor price "
                 f"({self.min_pct:g}-{self.max_pct:g}% of the floor then)"]
        if self.offers:
            parts.append(f"{self.offers} accepted {'offer' if self.offers == 1 else 'offers'} near the floor")
        if self.rare:
            parts.append(f"{self.rare} rare {'item' if self.rare == 1 else 'items'} (rarest {self.rare_pct:g}%)")
        if self.below:
            parts.append(f"{self.below} below")
        if self.above:
            parts.append(f"{self.above} above")
        if self.skipped:
            parts.append(f"{self.skipped} not comparable")
        return ", ".join(parts)


def _floor_at(ts: int, snap_ts: Sequence[int], snap_floor: Sequence[float], window: int,
              current_floor: float) -> Tuple[float, str]:
    """The floor just before a sale (a floor buy lifts the floor, so 'after' is the second choice)."""
    i = bisect_right(snap_ts, ts)
    if i > 0 and ts - snap_ts[i - 1] <= window:
        return snap_floor[i - 1], "before"
    if i < len(snap_ts) and snap_ts[i] - ts <= window:
        return snap_floor[i], "after"
    return current_floor, "now"


def _is_rare(rank: Optional[int], supply: Optional[int], rare_pct: float) -> bool:
    return bool(rank and rank > 0 and supply and supply > 0 and rare_pct > 0 and rank <= supply * rare_pct / 100.0)


def _sale_key(ev: SaleEvent) -> str:
    return ev.transaction or ev.order_hash or ev.event_id


def _wash_keys(events: Iterable[SaleEvent]) -> Set[str]:
    """Sales that look like trading with yourself: seller == buyer, or A->B and B->A in the same week."""
    pairs: Dict[Tuple[str, str], List[str]] = {}
    flagged: Set[str] = set()
    for ev in events:
        s, b = (ev.seller or "").lower(), (ev.buyer or "").lower()
        if not s or not b:
            continue
        if s == b:
            flagged.add(ev.event_id)
            continue
        pairs.setdefault((s, b), []).append(ev.event_id)
    for (s, b), ids in pairs.items():
        if (b, s) in pairs:
            flagged.update(ids)
    return flagged


def classify_floor_sales(
    events: Optional[List[SaleEvent]],
    start_ts: int,
    end_ts: int,
    current_floor: Optional[float],
    floor_currency: Optional[str],
    currency_groups: List[Set[str]],
    snapshots: Sequence[Dict] = (),
    min_pct: float = 90.0,
    max_pct: float = 115.0,
    count_mode: str = "transactions",
    window_hours: float = 6.0,
    order_info: Optional[OrderInfo] = None,
    rarity_rank: Optional[RarityRank] = None,
    supply: Optional[int] = None,
    rare_pct: float = 10.0,
) -> Optional[FloorSalesMetrics]:
    """
    Labels each sale in [start_ts, end_ts]. Returns None when the floor is unknown.
    Floor-priced sales are then checked with order_info (an accepted offer doesn't count) and rarity_rank
    (an item in the rarest rare_pct % of the supply doesn't count). Unknown answers leave the sale counted.
    """
    if events is None or not current_floor or current_floor <= 0:
        return None

    snaps = sorted((int(s["ts"]), float(s["floor_price"])) for s in snapshots
                   if s.get("floor_price") and float(s["floor_price"]) > 0
                   and (not s.get("currency") or same_currency(s.get("currency"), floor_currency, currency_groups)))
    snap_ts = [t for t, _ in snaps]
    snap_floor = [f for _, f in snaps]
    window = int(window_hours * 3600)

    in_window = [ev for ev in deduplicate_sale_events(events) if start_ts <= ev.event_timestamp <= end_ts]
    wash = _wash_keys(in_window)
    check_rarity = rarity_rank is not None and bool(supply) and rare_pct > 0
    m = FloorSalesMetrics(min_pct=min_pct, max_pct=max_pct, sale_rows=len(in_window),
                          rare_pct=rare_pct if check_rarity else 0.0)
    floor_keys: Set[str] = set()
    floor_items = 0
    all_keys = {_sale_key(ev) for ev in in_window}
    m.total = sum(max(1, ev.quantity or 1) for ev in in_window) if count_mode == "item_quantity" else len(all_keys)
    # Without a snapshot from just before the sale, the reference floor may already include the jump a floor
    # buy causes (the cheapest listing is gone), so allow more room below before calling it an accepted offer.
    # Offers sit at or under ~71% of the floor while the 40% spread rule is on, so 80% still keeps them out.
    loose_min = min(min_pct, 80.0)

    for ev in sorted(in_window, key=lambda e: e.event_timestamp, reverse=True):
        qty = max(1, ev.quantity or 1)
        price = ev.price_value / qty if ev.price_value is not None else None
        row = SaleRow(ts=ev.event_timestamp, price=price, currency=ev.price_currency,
                      ref_floor=None, ref_source="", pct=None, label=SKIPPED, event=ev)
        if price is None or price <= 0:
            row.note = "no price"
        elif not same_currency(ev.price_currency, floor_currency, currency_groups):
            row.note = "different coin"
        elif ev.event_id in wash:
            row.note = "same wallets on both sides"
            m.wash += 1
        else:
            ref, source = _floor_at(ev.event_timestamp, snap_ts, snap_floor, window, current_floor)
            row.ref_floor, row.ref_source = ref, source
            row.pct = price / ref * 100.0
            m.priced += 1
            if row.pct < (min_pct if source == "before" else loose_min):
                row.label = BELOW
                m.below += 1
            elif row.pct > max_pct:
                row.label = ABOVE
                m.above += 1
            else:
                info = order_info(ev) if order_info else None
                row.how = (info or {}).get("kind") or ""
                if row.how == OFFER_ACCEPTED:
                    row.label = OFFER
                    m.offers += 1
                else:
                    row.rank = rarity_rank(ev) if check_rarity else None
                    if _is_rare(row.rank, supply, rare_pct):
                        row.label = RARE
                        m.rare += 1
                    else:
                        row.label = FLOOR
                        floor_keys.add(_sale_key(ev))
                        floor_items += qty
        if row.label == SKIPPED:
            m.skipped += 1
        m.rows.append(row)

    m.floor_sales = floor_items if count_mode == "item_quantity" else len(floor_keys)
    return m


def too_few_floor_sales_reason(fs: FloorSalesMetrics, needed: int) -> str:
    if fs.total and not fs.priced:
        if fs.wash and fs.wash == fs.skipped:
            return "Only trades between the same wallets in the last 7 days"
        return "Sale prices couldn't be compared with the floor"
    if fs.floor_sales == 0:
        if fs.rare and not fs.offers:
            return "The only sales at floor price were rare items"
        if fs.offers and not fs.rare:
            return "Sales near the floor were accepted offers, not listings bought"
        if fs.offers and fs.rare:
            return "Sales near the floor were accepted offers or rare items"
        return "No sales at floor price in the last 7 days"
    return f"Only {fs.floor_sales} {'sale' if fs.floor_sales == 1 else 'sales'} at floor price in 7 days (needs {needed})"


@dataclass
class OfferSalesMetrics:
    offer_sales: int = 0   # sellers accepting a collection offer, counted like the pace rule
    confirmed: int = 0     # of those, OpenSea's order says so
    by_price: int = 0      # of those, order unknown but the price was below the floor (only an offer sells there)
    other_offers: int = 0  # accepted offers on a trait or one item: not the bid this strategy places
    total: int = 0         # sales in the window
    days: int = 14

    def summary(self, needed: int) -> Dict:
        return {"count": self.offer_sales, "needed": needed, "confirmed": self.confirmed, "by_price": self.by_price,
                "other_offers": self.other_offers, "total": self.total, "days": self.days}


def classify_offer_sales(
    events: Optional[List[SaleEvent]],
    start_ts: int,
    end_ts: int,
    current_floor: Optional[float],
    floor_currency: Optional[str],
    currency_groups: List[Set[str]],
    snapshots: Sequence[Dict] = (),
    min_pct: float = 90.0,
    max_pct: float = 115.0,
    count_mode: str = "transactions",
    window_hours: float = 6.0,
    order_info: Optional[OrderInfo] = None,
    days: int = 14,
) -> Optional[OfferSalesMetrics]:
    """
    Counts sales in [start_ts, end_ts] where a seller accepted a collection offer. Sales priced above the
    floor-price band are skipped (a collection offer never pays that much). When the order is unknown, a sale
    below the band counts, since only an offer sells under the floor. Returns None when the floor is unknown.
    """
    fs = classify_floor_sales(events, start_ts, end_ts, current_floor, floor_currency, currency_groups, snapshots,
                              min_pct=min_pct, max_pct=max_pct, count_mode=count_mode, window_hours=window_hours)
    if fs is None:
        return None
    m = OfferSalesMetrics(total=fs.total, days=days)
    keys: Set[str] = set()
    items = 0
    for row in fs.rows:  # newest first
        ev = row.event
        if ev is None or row.label not in (BELOW, FLOOR):
            continue
        info = order_info(ev) if order_info else None
        kind = (info or {}).get("kind")
        if kind == OFFER_ACCEPTED:
            if (info or {}).get("offer_type") in ("trait", "item"):
                m.other_offers += 1
                continue
            m.confirmed += 1
        elif kind is None and row.label == BELOW and (row.ref_source != "now" or (row.pct or 100) < 75):
            # Compared with today's floor (no saved floor from then), a listing bought at the old floor can look
            # "below" after the floor rose, so only a price far under today's floor counts without the order.
            m.by_price += 1
        else:
            continue
        keys.add(_sale_key(ev))
        items += max(1, ev.quantity or 1)
    m.offer_sales = items if count_mode == "item_quantity" else len(keys)
    return m


def too_few_offer_sales_reason(os_: OfferSalesMetrics, needed: int) -> str:
    d = os_.days
    if os_.offer_sales == 0:
        if os_.other_offers:
            return f"No collection offers accepted in {d} days (only offers on a trait or one item)"
        return f"No sales to a collection offer in the last {d} days"
    n = os_.offer_sales
    return f"Only {n} {'sale' if n == 1 else 'sales'} to a collection offer in {d} days (needs {needed})"
