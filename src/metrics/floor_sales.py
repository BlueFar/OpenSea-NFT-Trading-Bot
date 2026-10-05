"""
Floor-price sales: which of last week's sales were bought at about the floor price.

A collection can trade often while every sale is someone accepting the top offer. Those sales say
nothing about whether a listing a little under the floor will sell, which is how the strategy exits.
So each sale is compared with the floor at the time it happened:
  - floor:  paid min_pct..max_pct of that floor (default 90-115%) -> counts as a floor-price sale
  - below:  paid less (usually an accepted offer)
  - above:  paid more (usually a rare item)
  - skipped: no price, a coin that can't be compared with the floor, or a likely wash trade
"""
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..config.chains import same_currency
from ..models.collection import SaleEvent
from .calculator import deduplicate_sale_events

FLOOR, BELOW, ABOVE, SKIPPED = "floor", "below", "above", "skipped"
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

    def as_dict(self) -> Dict:
        return {"ts": self.ts, "price": self.price, "currency": self.currency, "ref_floor": self.ref_floor,
                "ref_source": self.ref_source, "pct": None if self.pct is None else round(self.pct, 1),
                "label": self.label, "note": self.note}


@dataclass
class FloorSalesMetrics:
    floor_sales: int = 0   # counted like the pace rule (distinct transactions, or items)
    priced: int = 0        # sales that could be compared with the floor
    below: int = 0
    above: int = 0
    skipped: int = 0
    total: int = 0         # sales in the window (each sale once)
    min_pct: float = 90.0
    max_pct: float = 115.0
    rows: List[SaleRow] = field(default_factory=list)

    def summary(self, needed: int) -> Dict:
        return {"count": self.floor_sales, "needed": needed, "priced": self.priced, "below": self.below,
                "above": self.above, "skipped": self.skipped, "total": self.total,
                "min_pct": self.min_pct, "max_pct": self.max_pct,
                "rows": [r.as_dict() for r in self.rows[:MAX_ROWS]]}

    def breakdown(self) -> str:
        parts = [f"{self.floor_sales} of {self.total} sales at floor price "
                 f"({self.min_pct:g}-{self.max_pct:g}% of the floor then)"]
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
) -> Optional[FloorSalesMetrics]:
    """Labels each sale in [start_ts, end_ts]. Returns None when the floor is unknown."""
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
    m = FloorSalesMetrics(min_pct=min_pct, max_pct=max_pct, total=len(in_window))
    floor_keys: Set[str] = set()
    floor_items = 0

    for ev in sorted(in_window, key=lambda e: e.event_timestamp, reverse=True):
        qty = max(1, ev.quantity or 1)
        price = ev.price_value / qty if ev.price_value is not None else None
        row = SaleRow(ts=ev.event_timestamp, price=price, currency=ev.price_currency,
                      ref_floor=None, ref_source="", pct=None, label=SKIPPED)
        if price is None or price <= 0:
            row.note = "no price"
        elif not same_currency(ev.price_currency, floor_currency, currency_groups):
            row.note = "different coin"
        elif ev.event_id in wash:
            row.note = "same wallets on both sides"
        else:
            ref, source = _floor_at(ev.event_timestamp, snap_ts, snap_floor, window, current_floor)
            row.ref_floor, row.ref_source = ref, source
            row.pct = price / ref * 100.0
            m.priced += 1
            if row.pct < min_pct:
                row.label = BELOW
                m.below += 1
            elif row.pct > max_pct:
                row.label = ABOVE
                m.above += 1
            else:
                row.label = FLOOR
                floor_keys.add(ev.transaction or ev.order_hash or ev.event_id)
                floor_items += qty
        if row.label == SKIPPED:
            m.skipped += 1
        m.rows.append(row)

    m.floor_sales = floor_items if count_mode == "item_quantity" else len(floor_keys)
    return m


def too_few_floor_sales_reason(fs: FloorSalesMetrics, needed: int) -> str:
    if fs.total and not fs.priced:
        return "Sale prices couldn't be compared with the floor"
    if fs.floor_sales == 0:
        return "No sales at floor price in the last 7 days"
    return f"Only {fs.floor_sales} {'sale' if fs.floor_sales == 1 else 'sales'} at floor price in 7 days (needs {needed})"
