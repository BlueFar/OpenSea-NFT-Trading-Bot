import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

# Statuses a position can have. "bidding" and "holding" are worked on by the bot; the rest are left alone.
ACTIVE_STATUSES = ("bidding", "holding")

POSITION_COLUMNS = (
    "slug", "name", "image", "chain", "currency", "status", "round", "rev",
    "bid", "bid_state", "competitor_top", "floor", "max_bid", "times_outbid", "highest_bid",
    "fee_pct", "royalty_pct", "gas",
    "entry_price", "entry_at", "entry_tx", "entry_token", "ask", "listed_at",
    "round_started_at", "last_tick_at", "last_sale_check", "note", "created_at", "updated_at",
)


class SimStore:
    """
    Paper-trading state, in the bot's own SQLite file (tables prefixed sim_).
    The bot writes positions with a revision check, so a Stop or Remove pressed on the dashboard
    while the bot is mid-check is never overwritten.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_db(self):
        with self._conn() as conn:
            c = conn.cursor()
            c.execute("PRAGMA journal_mode=WAL")
            # One virtual offer or listing per collection
            c.execute("""
                CREATE TABLE IF NOT EXISTS sim_positions (
                    slug TEXT PRIMARY KEY,
                    name TEXT, image TEXT, chain TEXT, currency TEXT,
                    status TEXT, round INTEGER DEFAULT 1, rev INTEGER DEFAULT 0,
                    bid REAL, bid_state TEXT, competitor_top REAL, floor REAL, max_bid REAL,
                    times_outbid INTEGER DEFAULT 0, highest_bid REAL,
                    fee_pct REAL, royalty_pct REAL, gas REAL,
                    entry_price REAL, entry_at INTEGER, entry_tx TEXT, entry_token TEXT,
                    ask REAL, listed_at INTEGER,
                    round_started_at INTEGER, last_tick_at INTEGER, last_sale_check INTEGER,
                    note TEXT, created_at INTEGER, updated_at INTEGER
                )
            """)
            # What the market and our offer or listing looked like at each check
            c.execute("""
                CREATE TABLE IF NOT EXISTS sim_ticks (
                    slug TEXT, ts INTEGER, round INTEGER, phase TEXT,
                    bid REAL, competitor_top REAL, floor REAL, ask REAL,
                    PRIMARY KEY (slug, ts)
                )
            """)
            # Completed round trips: bought with the offer, sold with the listing
            c.execute("""
                CREATE TABLE IF NOT EXISTS sim_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT, round INTEGER, currency TEXT,
                    entry_price REAL, entry_at INTEGER, entry_tx TEXT, entry_token TEXT,
                    exit_price REAL, exit_at INTEGER, exit_tx TEXT,
                    fees REAL, gas REAL, net REAL, roi_pct REAL, times_outbid INTEGER
                )
            """)
            # Plain-language log for the dashboard
            c.execute("""
                CREATE TABLE IF NOT EXISTS sim_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER, slug TEXT, kind TEXT, message TEXT
                )
            """)
            # Real sales already looked at, so each one is judged once
            c.execute("""
                CREATE TABLE IF NOT EXISTS sim_seen_sales (
                    slug TEXT, event_id TEXT, ts INTEGER,
                    PRIMARY KEY (slug, event_id)
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    @staticmethod
    def _row(r) -> Dict[str, Any]:
        return dict(zip(POSITION_COLUMNS, r))

    def get_positions(self, include_removed: bool = False) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {', '.join(POSITION_COLUMNS)} FROM sim_positions "
                                + ("" if include_removed else "WHERE status != 'removed' ")
                                + "ORDER BY created_at, slug").fetchall()
        return [self._row(r) for r in rows]

    def get_position(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {', '.join(POSITION_COLUMNS)} FROM sim_positions WHERE slug=?", (slug,)).fetchone()
        return self._row(r) if r else None

    def add_position(self, slug: str, note: str = "Added. The first offer goes in at the next check.") -> bool:
        """Starts simulating a collection. False if it is already there (a removed one is brought back)."""
        existing = self.get_position(slug)
        if existing is not None:
            if existing["status"] != "removed":
                return False
            self.set_status(slug, "start")
            return True
        now = int(time.time())
        with self._conn() as conn:
            conn.execute("INSERT INTO sim_positions (slug, name, status, round, rev, times_outbid, note, "
                         "created_at, updated_at) VALUES (?, ?, 'bidding', 1, 0, 0, ?, ?, ?)",
                         (slug, slug, note, now, now))
            conn.execute("INSERT INTO sim_events (ts, slug, kind, message) VALUES (?, ?, 'added', ?)", (now, slug, note))
            conn.commit()
        return True

    def set_status(self, slug: str, action: str) -> Optional[Dict[str, Any]]:
        """Dashboard buttons: stop, start or remove. Start resumes the listing if an item was bought."""
        pos = self.get_position(slug)
        if pos is None:
            return None
        now = int(time.time())
        if action == "stop":
            status, note = "stopped", "Stopped from the dashboard."
        elif action == "remove":
            status, note = "removed", "Removed from the dashboard."
        elif action == "start":
            if pos["entry_price"] is not None:
                status, note = "holding", "Started again: still holding the item bought."
            else:
                status, note = "bidding", "Started again: placing a new offer at the next check."
        else:
            raise ValueError(f"Unknown action {action}")
        with self._conn() as conn:
            fields = {"status": status, "note": note, "updated_at": now, "rev": (pos["rev"] or 0) + 1}
            if action == "start" and status == "bidding":
                # A fresh offer: sales from while it was stopped don't count
                fields.update({"bid": None, "bid_state": None, "round_started_at": None, "last_sale_check": None})
            if action == "start" and status == "holding":
                fields.update({"listed_at": None, "ask": None, "last_sale_check": None})
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE sim_positions SET {sets} WHERE slug=?", (*fields.values(), slug))
            conn.execute("INSERT INTO sim_events (ts, slug, kind, message) VALUES (?, ?, ?, ?)",
                         (now, slug, action, note))
            conn.commit()
        return self.get_position(slug)

    def save_round(self, pos: Dict[str, Any], expected_rev: int, events: Iterable[tuple] = (),
                   ticks: Iterable[tuple] = (), trades: Iterable[Dict[str, Any]] = (),
                   seen: Iterable[tuple] = ()) -> bool:
        """
        Writes one check's results in one transaction, only if nobody changed the position meanwhile
        (e.g. Stop pressed on the dashboard). Returns False, writing nothing, if they did.
        """
        now = int(time.time())
        pos = dict(pos)
        pos["rev"] = expected_rev + 1
        pos["updated_at"] = now
        cols = [c for c in POSITION_COLUMNS if c != "slug"]
        with self._conn() as conn:
            cur = conn.execute(f"UPDATE sim_positions SET {', '.join(f'{c}=?' for c in cols)} "
                               f"WHERE slug=? AND rev=?", (*[pos.get(c) for c in cols], pos["slug"], expected_rev))
            if cur.rowcount != 1:
                conn.rollback()
                return False
            conn.executemany("INSERT INTO sim_events (ts, slug, kind, message) VALUES (?, ?, ?, ?)", list(events))
            conn.executemany("INSERT OR REPLACE INTO sim_ticks (slug, ts, round, phase, bid, competitor_top, floor, ask) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", list(ticks))
            for t in trades:
                keys = list(t.keys())
                conn.execute(f"INSERT INTO sim_trades ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                             [t[k] for k in keys])
            conn.executemany("INSERT OR IGNORE INTO sim_seen_sales (slug, event_id, ts) VALUES (?, ?, ?)", list(seen))
            # Housekeeping: keep 30 days of checks and 3 days of judged sales
            conn.execute("DELETE FROM sim_ticks WHERE ts < ?", (now - 30 * 86400,))
            conn.execute("DELETE FROM sim_seen_sales WHERE ts < ?", (now - 3 * 86400,))
            conn.commit()
        return True

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def get_ticks(self, slug: str, round_no: Optional[int] = None, since_ts: int = 0) -> List[Dict[str, Any]]:
        q = "SELECT ts, round, phase, bid, competitor_top, floor, ask FROM sim_ticks WHERE slug=? AND ts>=?"
        args: List[Any] = [slug, since_ts]
        if round_no is not None:
            q += " AND round=?"
            args.append(round_no)
        with self._conn() as conn:
            rows = conn.execute(q + " ORDER BY ts", args).fetchall()
        return [dict(zip(("ts", "round", "phase", "bid", "competitor_top", "floor", "ask"), r)) for r in rows]

    def seen_sales(self, slug: str, event_ids: Iterable[str]) -> set:
        ids = [e for e in dict.fromkeys(event_ids) if e]
        out = set()
        with self._conn() as conn:
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                out.update(r[0] for r in conn.execute(
                    f"SELECT event_id FROM sim_seen_sales WHERE slug=? AND event_id IN ({','.join('?' * len(part))})",
                    [slug, *part]))
        return out

    def get_trades(self, limit: int = 200) -> List[Dict[str, Any]]:
        cols = ("id", "slug", "round", "currency", "entry_price", "entry_at", "entry_tx", "entry_token",
                "exit_price", "exit_at", "exit_tx", "fees", "gas", "net", "roi_pct", "times_outbid")
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {', '.join(cols)} FROM sim_trades ORDER BY exit_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def get_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT ts, slug, kind, message FROM sim_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "slug": r[1], "kind": r[2], "message": r[3]} for r in rows]

    def log(self, slug: str, kind: str, message: str):
        with self._conn() as conn:
            conn.execute("INSERT INTO sim_events (ts, slug, kind, message) VALUES (?, ?, ?, ?)",
                         (int(time.time()), slug, kind, message))
            conn.commit()

