"""
A collection's track record in the simulation: does our offer get taken, how fast does the bought item sell, and
does it make money. Each collection gets a verdict the real bot can later trade on:

- proven:   at least MIN_SALES_PROVEN sales, none at a loss, and in profit overall (held items at today's floor)
- losing:   down overall, counting held items at today's floor, after a losing trade or an item held under
            water for UNDERWATER_DAYS (a fresh item a little under is not judged yet)
- slow:     no fill after SLOW_DAYS of bidding
- not_yet:  everything else (too few trades to judge)
"""
from typing import Any, Dict, List, Optional

from .engine import describe_position, fmt_price

MIN_SALES_PROVEN = 2
SLOW_DAYS = 14.0
UNDERWATER_DAYS = 3.0
VERDICT_ORDER = {"proven": 0, "not_yet": 1, "slow": 2, "losing": 3}
DAY = 86400.0


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def track_record(pos: Dict[str, Any], trades: List[Dict[str, Any]], exit_discount_pct: float,
                 now: int) -> Dict[str, Any]:
    """
    pos: one strategy's position for the collection; trades: all its trades for that collection (sold, and
    removed items valued at the floor).
    """
    d = describe_position(pos, exit_discount_pct)
    cur = pos.get("currency") or (trades[0].get("currency") if trades else None)
    sold = [t for t in trades if t.get("how") == "sold"]
    holding = pos.get("entry_price") is not None and pos.get("status") != "removed"
    fills = len(trades) + (1 if holding else 0)
    bid_days = (pos.get("bid_seconds") or 0.0) / DAY

    sell_days = [max(0.0, (t["exit_at"] - t["entry_at"]) / DAY) for t in sold if t.get("exit_at") and t.get("entry_at")]
    realised = sum(t.get("net") or 0.0 for t in trades)
    open_net = d.get("now_net") if holding else None
    total = realised + (open_net or 0.0) if fills else None
    losses = [t for t in trades if (t.get("net") or 0.0) < 0]
    roi = [t["roi_pct"] for t in sold if t.get("roi_pct") is not None]
    held_days = (now - pos["entry_at"]) / DAY if holding and pos.get("entry_at") else None

    long_under = held_days is not None and held_days >= UNDERWATER_DAYS and (open_net or 0) < 0
    if fills and total is not None and total < 0 and (losses or long_under):
        verdict = "losing"
        why = (f"{len(losses)} of {len(trades)} trades lost money" if losses else
               f"the item held for {held_days:.0f} days is down {fmt_price(abs(open_net or 0), cur)} at today's floor")
    elif len(sold) >= MIN_SALES_PROVEN and not losses and (total or 0) > 0:
        verdict = "proven"
        why = (f"{len(sold)} sales, all profitable, {_mean(roi):+.0f}% a trade on average" if roi else
               f"{len(sold)} profitable sales")
    elif not fills and bid_days >= SLOW_DAYS:
        verdict, why = "slow", f"no fill in {bid_days:.0f} days of bidding"
    else:
        verdict = "not_yet"
        if not fills:
            why = f"no fill yet ({bid_days:.1f} days of bidding)"
        elif not sold:
            why = f"holding its first item ({held_days:.1f} days)" if held_days is not None else "first item bought"
            if open_net is not None and open_net < 0:
                why += f", down {fmt_price(abs(open_net), cur)} at today's floor"
        else:
            why = f"{len(sold)} profitable sale{'s' if len(sold) != 1 else ''} so far; proven after {MIN_SALES_PROVEN}"

    return {
        "verdict": verdict,
        "why": why,
        "fills": fills,
        "sales": len(sold),
        "bid_days": round(bid_days, 2),
        "fills_per_week": round(fills / bid_days * 7, 2) if bid_days >= 1 else None,
        "days_to_sell": round(_mean(sell_days), 2) if sell_days else None,
        "roi_per_trade": round(_mean(roi), 1) if roi else None,
        # Profit for each day an item was held: tells a quick 20% apart from a slow 20%
        "roi_per_day": round(sum(roi) / sum(sell_days), 2) if roi and sum(sell_days) > 0 else None,
        "total_net": total,
        "currency": cur,
        "underwater": bool(holding and open_net is not None and open_net < 0),
        "missed_fills": (pos.get("missed_fills") or 0) + (pos.get("missed_above_limit") or 0),
    }


def sort_key(rec: Dict[str, Any]):
    """Best first: proven before unproven, then faster profit, then more fills."""
    return (VERDICT_ORDER.get(rec["verdict"], 9), -(rec.get("roi_per_day") or 0.0), -(rec.get("fills_per_week") or 0.0))
