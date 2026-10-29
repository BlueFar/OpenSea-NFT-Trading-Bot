"""
The $50-$500 view of the simulation, and the Buy List.

The $50-$500 page is a filtered view of strategy A: no extra OpenSea calls, and the history from the start of
the simulation shows at once. A collection is in it when our offer (or, once bought, what we paid) is between
band_min_usd and band_max_usd. A bought item's dollar price is fixed the first time the dashboard sees it, so it
doesn't move in and out of the page as the coin moves.

The Buy List is what the trading bot (not built yet) will trade. The owner adds collections by hand; nothing is
added or removed automatically. Each row keeps the trading settings the simulation used when it was added, an
on/pause switch, and there is one "pause all" switch. The bot will only trade rows that are on while pause all
is off. A "ready" checklist is shown beside every collection, but a collection can be added either way.
"""
import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

from ..utils.prices import canonical
from .engine import Costs, describe_position, trade_result
from .store import SALE_KINDS

DAY = 86400.0


class BuyListStore:
    """The Buy List and the fixed dollar prices of bought items, in the bot's own SQLite file."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS buy_list (
                    slug TEXT PRIMARY KEY, added_at INTEGER, added_by TEXT, note TEXT,
                    enabled INTEGER DEFAULT 1,
                    settings TEXT,        -- the simulation's trading settings when it was added (JSON)
                    floor_at_add REAL, currency TEXT,
                    ready_at_add INTEGER  -- 1 if every readiness check passed when it was added
                )
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS buy_meta (key TEXT PRIMARY KEY, value TEXT)")
            # Dollar price of the coin when an item was bought: key = "A:slug:entry_at"
            conn.execute("CREATE TABLE IF NOT EXISTS usd_at_entry (key TEXT PRIMARY KEY, rate REAL, at INTEGER)")
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10.0)

    # -- Buy List --------------------------------------------------------
    COLS = ("slug", "added_at", "added_by", "note", "enabled", "settings", "floor_at_add", "currency", "ready_at_add")

    def get_items(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {', '.join(self.COLS)} FROM buy_list ORDER BY added_at, slug").fetchall()
        out = []
        for r in rows:
            d = dict(zip(self.COLS, r))
            d["enabled"] = bool(d["enabled"])
            d["ready_at_add"] = bool(d["ready_at_add"])
            try:
                d["settings"] = json.loads(d["settings"]) if d["settings"] else {}
            except ValueError:
                d["settings"] = {}
            out.append(d)
        return out

    def slugs(self) -> List[str]:
        return [i["slug"] for i in self.get_items()]

    def add(self, slug: str, settings: Dict[str, Any], floor: Optional[float], currency: Optional[str],
            ready: bool, note: str = "", added_by: str = "you") -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO buy_list (slug, added_at, added_by, note, enabled, settings, floor_at_add, "
                "currency, ready_at_add) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (slug, int(time.time()), added_by, note or "", json.dumps(settings, sort_keys=True), floor, currency,
                 1 if ready else 0))
            conn.commit()
            return cur.rowcount == 1

    def remove(self, slug: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM buy_list WHERE slug=?", (slug,))
            conn.commit()
            return cur.rowcount == 1

    def set_enabled(self, slug: str, enabled: bool) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE buy_list SET enabled=? WHERE slug=?", (1 if enabled else 0, slug))
            conn.commit()
            return cur.rowcount == 1

    def paused_all(self) -> bool:
        with self._conn() as conn:
            r = conn.execute("SELECT value FROM buy_meta WHERE key='paused_all'").fetchone()
        return bool(r and r[0] == "1")

    def set_paused_all(self, paused: bool) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO buy_meta (key, value) VALUES ('paused_all', ?)", ("1" if paused else "0",))
            conn.commit()

    def tradable(self) -> List[str]:
        """What the trading bot may trade: rows switched on, and nothing while pause all is on."""
        if self.paused_all():
            return []
        return [i["slug"] for i in self.get_items() if i["enabled"]]

    # -- Dollar price at the time of buying ------------------------------
    def entry_rates(self, keys_rates: Iterable[tuple]) -> Dict[str, float]:
        """
        keys_rates: (key, today's rate or None). Returns the fixed rate for each key, saving today's rate for a
        key seen for the first time (so the dollar price is the one from about when it was bought).
        """
        pairs = list(keys_rates)
        if not pairs:
            return {}
        out: Dict[str, float] = {}
        now = int(time.time())
        with self._conn() as conn:
            keys = [k for k, _ in pairs]
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                for k, rate in conn.execute(f"SELECT key, rate FROM usd_at_entry WHERE key IN ({','.join('?' * len(part))})", part):
                    out[k] = rate
            new = [(k, r, now) for k, r in pairs if k not in out and r]
            if new:
                conn.executemany("INSERT OR IGNORE INTO usd_at_entry (key, rate, at) VALUES (?, ?, ?)", new)
                conn.commit()
                out.update({k: r for k, r, _ in new})
        return out


def entry_key(strategy: str, slug: str, entry_at: Optional[int]) -> str:
    return f"{strategy}:{slug}:{int(entry_at or 0)}"


def market_facts(db_path: str, slugs: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """
    From the bot's latest rule check of each collection that got as far as reading its sales: sales in the 7 days
    before that check, and the latest sale at about the floor price (90-115% of the floor, not a rare item and
    not an accepted offer).
    """
    wanted = list(dict.fromkeys(slugs))
    out: Dict[str, Dict[str, Any]] = {}
    if not wanted:
        return out
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            for i in range(0, len(wanted), 400):
                part = wanted[i:i + 400]
                rows = conn.execute(
                    "SELECT h.slug, h.details, h.date_str FROM candidate_history h JOIN ("
                    f"  SELECT slug, MAX(date_str) d FROM candidate_history WHERE slug IN ({','.join('?' * len(part))})"
                    "  AND details LIKE '%\"floor_sales\"%' GROUP BY slug) m ON h.slug = m.slug AND h.date_str = m.d", part).fetchall()
                for slug, details, day in rows:
                    out[slug] = _facts(details, day)
    except sqlite3.OperationalError:  # the bot hasn't created its tables yet
        return {}
    return out


def _facts(details: Optional[str], day: Optional[str]) -> Dict[str, Any]:
    try:
        d = json.loads(details) if details else {}
    except ValueError:
        d = {}
    fs = d.get("floor_sales") or {}
    rows = [r for r in fs.get("rows") or [] if r.get("label") == "floor" and isinstance(r.get("price"), (int, float))]
    last = max(rows, key=lambda r: r.get("ts") or 0) if rows else None
    return {"sales_7d": fs.get("total") if isinstance(fs.get("total"), int) else None,
            "last_sale": last["price"] if last else None, "last_sale_currency": last.get("currency") if last else None,
            "last_sale_at": last.get("ts") if last else None, "checked_on": day}


def _same_coin(a: Optional[str], b: Optional[str]) -> bool:
    return canonical(a) == canonical(b)  # WETH and ETH are the same money


def held_value(pos: Dict[str, Any], facts: Optional[Dict[str, Any]], exit_discount_pct: float) -> Optional[Dict[str, Any]]:
    """
    What a held item would bring today, after OpenSea fee, royalty and gas: at the lower of the last real
    floor-price sale and our usual price under today's floor, so a thin collection's floor doesn't flatter it.
    In a stop-loss, at our stop-loss listing.
    """
    entry, floor = pos.get("entry_price"), pos.get("floor")
    if not entry or pos.get("fee_pct") is None:
        return None
    costs = Costs(pos["fee_pct"], pos.get("royalty_pct") or 0.0, pos.get("gas") or 0.0, exit_discount_pct)
    prices, basis = [], None
    if floor:
        prices.append((floor * (1 - exit_discount_pct / 100.0), "floor"))
    last = (facts or {}).get("last_sale")
    if last and _same_coin((facts or {}).get("last_sale_currency"), pos.get("currency")):
        prices.append((last, "last_sale"))
    if pos.get("stop_loss_at") and pos.get("ask"):
        prices = [(pos["ask"], "stop_loss")]
    if not prices:
        return None
    price, basis = min(prices)
    r = trade_result(entry, price, costs)
    return {"price": price, "basis": basis, "net": r["net"], "value": entry + r["net"], "roi_pct": r["roi_pct"]}


def readiness(pos: Optional[Dict[str, Any]], trades: List[Dict[str, Any]], on_final: bool,
              facts: Optional[Dict[str, Any]], offer_usd: Optional[float], chain: Optional[str], cfg,
              slug: Optional[str] = None) -> Dict[str, Any]:
    """
    The checklist shown beside each collection. ok is True, False, or None (not known yet). cfg: BuyListConfig.
    """
    rc = cfg.ready
    sold = [t for t in trades if t.get("how") in SALE_KINDS]
    wins = [t for t in sold if (t.get("net") or 0) > 0]
    bid_days = ((pos or {}).get("bid_seconds") or 0.0) / DAY
    checks = []

    checks.append({"key": "fundamentals", "label": "Passed the fundamentals check (on the Final list)", "ok": on_final})

    lo, hi = cfg.band_min_usd, cfg.band_max_usd
    checks.append({"key": "band", "label": f"Offer between ${lo:,.0f} and ${hi:,.0f}",
                   "ok": None if offer_usd is None else lo <= offer_usd <= hi,
                   "detail": f"${offer_usd:,.0f}" if offer_usd is not None else "no dollar price yet"})

    all_won = len(wins) == len(sold)
    track_ok = bool(sold) and all_won and (len(sold) >= rc.min_rounds or (len(sold) >= 1 and bid_days >= rc.alt_bid_days))
    detail = (f"{len(sold)} round{'s' if len(sold) != 1 else ''}, {len(wins)} profitable, "
              f"{bid_days:.1f} days of bidding")
    checks.append({"key": "track", "label": f"{rc.min_rounds}+ rounds, all profitable (or {rc.alt_bid_days:g}+ days of "
                                            f"bidding with 1 profitable round)", "ok": track_ok, "detail": detail})

    s7 = (facts or {}).get("sales_7d")
    checks.append({"key": "sales", "label": f"At least {rc.min_sales_7d} sales in the last 7 days",
                   "ok": None if s7 is None else s7 >= rc.min_sales_7d,
                   "detail": "not checked yet" if s7 is None else
                   f"{s7} sale{'s' if s7 != 1 else ''}" + (f" (bot's check of {facts['checked_on']})" if facts.get("checked_on") else "")})

    slug = slug or (pos or {}).get("slug")
    risky_chain = (chain or "").lower() in {c.lower() for c in rc.risky_chains}
    why = rc.risky_collections.get(slug) if slug else None
    checks.append({"key": "chain", "label": "No known shutdown or chain risk", "ok": not (risky_chain or why),
                   "detail": why or (f"{chain} is on the risky chains list" if risky_chain else None)})

    return {"ready": all(c["ok"] is True for c in checks), "checks": checks,
            "failing": [c["label"] for c in checks if c["ok"] is not True]}


def bot_settings(sim_cfg, exit_discount_pct: float) -> Dict[str, Any]:
    """The trading settings saved with a Buy List row: what the simulation was doing when it was added."""
    return {
        "strategy": "A",
        "min_profit_gap_pct": sim_cfg.min_profit_gap_pct,
        "outbid_step_pct": sim_cfg.outbid_step_pct,
        "exit_discount_pct": exit_discount_pct,
        "repeat": sim_cfg.repeat,
        "exit": sim_cfg.exit.model_dump(exclude={"own_wallets"}),
    }


def in_band(usd: Optional[float], cfg) -> bool:
    return usd is not None and cfg.band_min_usd <= usd <= cfg.band_max_usd


def build_band(positions: List[Dict[str, Any]], trades: List[Dict[str, Any]], rate_now, entry_rates: Dict[str, float],
               facts: Dict[str, Dict[str, Any]], final: Iterable[str], buy: Iterable[str], cfg, exit_discount_pct: float,
               now: int, ready_cfg=None) -> Dict[str, Any]:
    """
    Everything the $50-$500 page shows, in dollars. positions/trades: strategy A. rate_now(currency) -> today's
    dollar rate or None. entry_rates: fixed rates by entry_key(). ready_cfg: the readiness rules when cfg's
    dollar range is widened (the Final list and Buy List pages show their collections at any price).
    """
    final, buy = set(final), set(buy)
    by_slug: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        by_slug.setdefault(t["slug"], []).append(t)

    def fixed(slug, entry_at, cur):
        r = entry_rates.get(entry_key("A", slug, entry_at))
        return r if r else rate_now(cur)

    bids, held, done = [], [], []
    for p in positions:
        d = describe_position(p, exit_discount_pct)
        cur = p.get("currency")
        if p["status"] == "bidding" and p.get("bid"):
            r = rate_now(cur)
            usd = p["bid"] * r if r else None
            cooling = (p.get("cool_off_until") or 0) > now
            if in_band(usd, cfg) and not cooling:
                bids.append({"slug": p["slug"], "name": p.get("name") or p["slug"], "image": p.get("image"),
                             "chain": p.get("chain"), "currency": cur, "bid": p["bid"], "bid_usd": usd,
                             "bid_state": p.get("bid_state"), "competitor_top": p.get("competitor_top"),
                             "floor": p.get("floor"), "floor_usd": p["floor"] * r if p.get("floor") and r else None,
                             "gap_pct": d.get("gap_pct"), "est_net_usd": d["est_net"] * r if d.get("est_net") is not None and r else None,
                             "bid_days": round((p.get("bid_seconds") or 0) / DAY, 1),
                             "placed_at": p.get("round_started_at") or p.get("created_at"),
                             "times_outbid": p.get("times_outbid") or 0, "round": p.get("round") or 1})
        elif p.get("entry_price") is not None and p["status"] in ("holding", "stopped"):
            r = fixed(p["slug"], p.get("entry_at"), cur)
            cost = p["entry_price"] * r if r else None
            if not in_band(cost, cfg):
                continue
            hv = held_value(p, facts.get(p["slug"]), exit_discount_pct)
            rn = rate_now(cur)
            days_held = (now - p["entry_at"]) / DAY if p.get("entry_at") else None
            held.append({"slug": p["slug"], "name": p.get("name") or p["slug"], "image": p.get("image"),
                         "chain": p.get("chain"), "currency": cur, "entry_price": p["entry_price"], "cost_usd": cost,
                         "entry_at": p.get("entry_at"), "days_held": round(days_held, 1) if days_held is not None else None,
                         "long_hold": days_held is not None and days_held >= cfg.long_hold_days,
                         "break_even": d.get("break_even"), "ask": p.get("ask"), "floor": p.get("floor"),
                         "value": hv["value"] if hv else None, "value_basis": hv["basis"] if hv else None,
                         "value_usd": hv["value"] * rn if hv and rn else None,
                         "net_usd": (hv["value"] * rn - cost) if hv and rn and cost is not None else None,
                         "stop_loss_at": p.get("stop_loss_at"), "under_since": p.get("under_since"),
                         "sl_steps": p.get("sl_steps") or 0, "status": p["status"]})

    for t in trades:
        if t.get("how") not in SALE_KINDS:
            continue  # "removed" is a valuation, not a sale
        r = fixed(t["slug"], t.get("entry_at"), t.get("currency"))
        cost = t["entry_price"] * r if r and t.get("entry_price") else None
        if not in_band(cost, cfg):
            continue
        done.append({"slug": t["slug"], "currency": t.get("currency"), "entry_price": t["entry_price"],
                     "exit_price": t.get("exit_price"), "entry_at": t.get("entry_at"), "exit_at": t.get("exit_at"),
                     "cost_usd": cost, "net": t.get("net"), "net_usd": (t.get("net") or 0) * r, "roi_pct": t.get("roi_pct"),
                     "days_held": round(((t.get("exit_at") or 0) - (t.get("entry_at") or 0)) / DAY, 1), "how": t.get("how")})
    done.sort(key=lambda t: t.get("exit_at") or 0, reverse=True)

    # Money panel
    offers = sum(b["bid_usd"] for b in bids)
    held_cost = sum(h["cost_usd"] for h in held)
    held_val = sum(h["value_usd"] for h in held if h["value_usd"] is not None)
    held_unvalued = sum(1 for h in held if h["value_usd"] is None)
    wins = [t for t in done if t["net_usd"] > 0]
    losses = [t for t in done if t["net_usd"] < 0]
    win_usd = sum(t["net_usd"] for t in wins)
    loss_usd = sum(t["net_usd"] for t in losses)
    realised = win_usd + loss_usd
    money = {
        "offers_usd": offers, "offers": len(bids),
        "offers_on_top": sum(1 for b in bids if b["bid_state"] in ("top", "alone")),
        "held_cost_usd": held_cost, "held_value_usd": held_val, "held": len(held), "held_unvalued": held_unvalued,
        "win_usd": win_usd, "wins": len(wins), "loss_usd": loss_usd, "losses": len(losses), "sales": len(done),
        "realised_usd": realised,
        # Profit from sales, minus the money still in held items and in open offers
        "after_tied_up_usd": realised - held_cost - offers,
        # Profit from sales, plus or minus what held items would bring today
        "total_usd": realised + (held_val - held_cost),
        "worst_case_usd": offers,
    }

    # Per-collection scorecard: everything the page shows for the collection
    pos_by = {p["slug"]: p for p in positions}
    slugs = list(dict.fromkeys([b["slug"] for b in bids] + [h["slug"] for h in held] + [t["slug"] for t in done]))
    names = {p["slug"]: p.get("name") or p["slug"] for p in positions}
    score = []
    for s in slugs:
        ts = [t for t in done if t["slug"] == s]
        p = pos_by.get(s) or {}
        bid_days = (p.get("bid_seconds") or 0) / DAY
        hold_days = sum(t["days_held"] for t in ts)
        fills = len(ts) + (1 if any(h["slug"] == s for h in held) else 0)
        offer_usd = next((b["bid_usd"] for b in bids if b["slug"] == s), None) or \
            next((h["cost_usd"] for h in held if h["slug"] == s), None) or (ts[0]["cost_usd"] if ts else None)
        ready = readiness(p or None, by_slug.get(s, []), s in final, facts.get(s), offer_usd, p.get("chain"),
                          ready_cfg or cfg, slug=s)
        score.append({"slug": s, "name": names.get(s, s), "image": p.get("image"), "chain": p.get("chain"),
                      "rounds": len(ts), "wins": sum(1 for t in ts if t["net_usd"] > 0),
                      "profit_usd": sum(t["net_usd"] for t in ts),
                      "pct_per_day": round(sum(t["roi_pct"] or 0 for t in ts) / hold_days, 2) if hold_days > 0 else None,
                      "days_per_fill": round(bid_days / fills, 1) if fills and bid_days else None,
                      "avg_days_to_sell": round(hold_days / len(ts), 1) if ts else None,
                      "bid_days": round(bid_days, 1), "fills": fills, "sales_7d": (facts.get(s) or {}).get("sales_7d"),
                      "final": s in final, "on_buy_list": s in buy, "ready": ready})
    score.sort(key=lambda r: (not r["ready"]["ready"], -r["rounds"], -r["profit_usd"]))
    for rows in (bids, held, done):
        for r in rows:
            r["name"] = names.get(r["slug"], r["slug"])
            r["final"] = r["slug"] in final
            r["on_buy_list"] = r["slug"] in buy
    bids.sort(key=lambda b: -b["bid_usd"])
    held.sort(key=lambda h: -(h["days_held"] or 0))
    return {"money": money, "bids": bids, "held": held, "trades": done, "scorecard": score,
            "band": {"min_usd": cfg.band_min_usd, "max_usd": cfg.band_max_usd, "long_hold_days": cfg.long_hold_days}}


def buy_warnings(item: Dict[str, Any], pos: Optional[Dict[str, Any]], trades: List[Dict[str, Any]],
                 ready: Dict[str, Any], cfg, now: int) -> List[Dict[str, str]]:
    """Amber and red flags for a Buy List row. They only warn: nothing is removed automatically."""
    out = []
    if not ready["ready"]:
        if item.get("ready_at_add"):
            out.append({"level": "bad", "text": "No longer passes: " + "; ".join(ready["failing"])})
        else:
            out.append({"level": "warn", "text": "Added, not ready: " + "; ".join(ready["failing"])})
    if pos is None or pos.get("status") == "removed":
        out.append({"level": "warn", "text": "Not in the simulation now, so there is no fresh market data"})
    elif pos.get("stop_loss_at"):
        out.append({"level": "bad", "text": "Its simulated item is in a stop-loss"})
    added = item.get("added_at") or 0
    if any(str(t.get("how") or "").startswith("stoploss") and (t.get("exit_at") or 0) >= added for t in trades):
        out.append({"level": "bad", "text": "A stop-loss sale happened in the simulation since it was added"})
    f0, f1 = item.get("floor_at_add"), (pos or {}).get("floor")
    if f0 and f1 and _same_coin(item.get("currency"), (pos or {}).get("currency")):
        drop = (1 - f1 / f0) * 100
        if drop >= cfg.floor_drop_warn_pct:
            out.append({"level": "warn", "text": f"Floor is {drop:.0f}% under the floor when it was added"})
    if pos and pos.get("entry_price") is not None and pos.get("entry_at") and \
            (now - pos["entry_at"]) / DAY >= cfg.long_hold_days:
        out.append({"level": "warn", "text": f"Simulated item held {((now - pos['entry_at']) / DAY):.0f} days without selling"})
    return out
