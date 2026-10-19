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
    "source", "auto_stopped", "leaving", "trial_round", "trial_state", "trial_started_at",
    "last_outbid_at", "missed_fills", "missed_above_limit", "limit_pct", "capped_since",
    "bid_seconds", "entry_metrics",
)
# Added after the first release: (column, type) brought into older databases
LATER_COLUMNS = (("leaving", "INTEGER DEFAULT 0"), ("trial_round", "INTEGER"), ("trial_state", "TEXT"),
                 ("trial_started_at", "INTEGER"), ("last_outbid_at", "INTEGER"),
                 ("missed_fills", "INTEGER DEFAULT 0"), ("missed_above_limit", "INTEGER DEFAULT 0"),
                 ("limit_pct", "REAL"), ("capped_since", "INTEGER"),
                 ("bid_seconds", "REAL DEFAULT 0"), ("entry_metrics", "TEXT"))
# Longest gap between two checks counted as time spent bidding (the bot may have been off)
MAX_CHECK_GAP_SECONDS = 3600
# Strategy A (the rules shown on the dashboard) keeps the original sim_ tables; strategy B, a second set of
# virtual offers judged on the same market data, has its own copy under simb_.
STRATEGY_PREFIX = {"A": "sim_", "B": "simb_"}


class SimStore:
    """
    Paper-trading state for one strategy, in the bot's own SQLite file (tables prefixed sim_ for strategy A,
    simb_ for B). The Final list is shared. The bot writes positions with a revision check, so a Stop or Remove
    pressed on the dashboard while the bot is mid-check is never overwritten.
    """

    def __init__(self, db_path: str, strategy: str = "A"):
        self.db_path = db_path
        self.strategy = strategy
        pre = STRATEGY_PREFIX[strategy]
        self.T_POS, self.T_TICKS, self.T_TRADES = pre + "positions", pre + "ticks", pre + "trades"
        self.T_EVENTS, self.T_SEEN = pre + "events", pre + "seen_sales"
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
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.T_POS} (
                    slug TEXT PRIMARY KEY,
                    name TEXT, image TEXT, chain TEXT, currency TEXT,
                    status TEXT, round INTEGER DEFAULT 1, rev INTEGER DEFAULT 0,
                    bid REAL, bid_state TEXT, competitor_top REAL, floor REAL, max_bid REAL,
                    times_outbid INTEGER DEFAULT 0, highest_bid REAL,
                    fee_pct REAL, royalty_pct REAL, gas REAL,
                    entry_price REAL, entry_at INTEGER, entry_tx TEXT, entry_token TEXT,
                    ask REAL, listed_at INTEGER,
                    round_started_at INTEGER, last_tick_at INTEGER, last_sale_check INTEGER,
                    note TEXT, created_at INTEGER, updated_at INTEGER,
                    source TEXT, auto_stopped INTEGER DEFAULT 0
                )
            """)
            have = {r[1] for r in c.execute(f"PRAGMA table_info({self.T_POS})")}
            new_cols = {col for col, _ in LATER_COLUMNS} - have
            for col, kind in LATER_COLUMNS:
                if col not in have:
                    try:
                        c.execute(f"ALTER TABLE {self.T_POS} ADD COLUMN {col} {kind}")
                    except sqlite3.OperationalError as e:  # the bot and the dashboard upgrading at once
                        if "duplicate column" not in str(e):
                            raise
            # What the market and our offer or listing looked like at each check
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.T_TICKS} (
                    slug TEXT, ts INTEGER, round INTEGER, phase TEXT,
                    bid REAL, competitor_top REAL, floor REAL, ask REAL,
                    PRIMARY KEY (slug, ts)
                )
            """)
            # Completed round trips: bought with the offer, sold with the listing
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.T_TRADES} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT, round INTEGER, currency TEXT,
                    entry_price REAL, entry_at INTEGER, entry_tx TEXT, entry_token TEXT,
                    exit_price REAL, exit_at INTEGER, exit_tx TEXT,
                    fees REAL, gas REAL, net REAL, roi_pct REAL, times_outbid INTEGER,
                    how TEXT
                )
            """)
            trade_cols = {r[1] for r in c.execute(f"PRAGMA table_info({self.T_TRADES})")}
            if "entry_metrics" not in trade_cols:
                try:
                    c.execute(f"ALTER TABLE {self.T_TRADES} ADD COLUMN entry_metrics TEXT")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
            if "bid_seconds" in new_cols:
                self._backfill_bid_seconds(c)
            # Plain-language log for the dashboard
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.T_EVENTS} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER, slug TEXT, kind TEXT, message TEXT
                )
            """)
            # Real sales already looked at, so each one is judged once
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.T_SEEN} (
                    slug TEXT, event_id TEXT, ts INTEGER,
                    PRIMARY KEY (slug, event_id)
                )
            """)
            # Collections that passed the fundamentals check (Final list page)
            c.execute("""
                CREATE TABLE IF NOT EXISTS final_list (
                    slug TEXT PRIMARY KEY, added_at INTEGER, added_by TEXT, note TEXT
                )
            """)
            c.execute("CREATE TABLE IF NOT EXISTS sim_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()

    def _backfill_bid_seconds(self, c) -> None:
        """Time spent bidding before it was counted, from the checks still kept (30 days)."""
        try:
            rows = c.execute(f"""
                SELECT slug, SUM(MIN(gap, {MAX_CHECK_GAP_SECONDS})) FROM (
                    SELECT slug, ts - LAG(ts) OVER w AS gap, LAG(phase) OVER w AS was
                    FROM {self.T_TICKS} WINDOW w AS (PARTITION BY slug ORDER BY ts)
                ) WHERE was='bidding' AND gap IS NOT NULL GROUP BY slug
            """).fetchall()
        except sqlite3.OperationalError:  # an SQLite without window functions: count from now on
            return
        c.executemany(f"UPDATE {self.T_POS} SET bid_seconds=? WHERE slug=?", [(s or 0, slug) for slug, s in rows])

    # ------------------------------------------------------------------
    # Final list and candidates
    # ------------------------------------------------------------------
    def get_final_list(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT slug, added_at, added_by, note FROM final_list ORDER BY added_at, slug").fetchall()
        return [dict(zip(("slug", "added_at", "added_by", "note"), r)) for r in rows]

    def add_final(self, slug: str, added_by: str = "you", note: str = "") -> bool:
        with self._conn() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO final_list (slug, added_at, added_by, note) VALUES (?, ?, ?, ?)",
                               (slug, int(time.time()), added_by, note or ""))
            conn.commit()
            return cur.rowcount == 1

    def seed_final(self, picks: Dict[str, str]) -> None:
        """Fills the Final list once from the settings file; later removals on the dashboard stick."""
        with self._conn() as conn:
            if conn.execute("SELECT 1 FROM sim_meta WHERE key='final_seeded'").fetchone():
                return
            now = int(time.time())
            for slug, note in picks.items():
                conn.execute("INSERT OR IGNORE INTO final_list (slug, added_at, added_by, note) VALUES (?, ?, 'claude', ?)",
                             (slug.strip().lower(), now, note or ""))
            conn.execute("INSERT INTO sim_meta (key, value) VALUES ('final_seeded', ?)", (str(now),))
            conn.commit()

    def remove_final(self, slug: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM final_list WHERE slug=?", (slug,))
            conn.commit()
            return cur.rowcount == 1

    def passing_candidates(self) -> List[str]:
        """Collections whose latest bot check passed every rule (the Candidates page)."""
        try:
            with self._conn() as conn:
                return [r[0] for r in conn.execute(
                    "SELECT slug FROM monitored_collections WHERE last_result_pass=1 ORDER BY slug")]
        except sqlite3.OperationalError:  # the bot hasn't created its tables yet
            return []

    def failing_candidates(self) -> List[str]:
        """Collections whose latest bot check failed a rule (so they're not on the Candidates page)."""
        try:
            with self._conn() as conn:
                return [r[0] for r in conn.execute(
                    "SELECT slug FROM monitored_collections WHERE last_result_pass=0 ORDER BY slug")]
        except sqlite3.OperationalError:
            return []

    def auto_remove(self, slug: str, note: str) -> None:
        """Takes out a collection that left the candidate list; it comes back if it passes again."""
        if self.set_status(slug, "remove", note=note) is not None:
            self.set_fields(slug, auto_stopped=1)

    def set_fields(self, slug: str, event: Optional[tuple] = None, **fields: Any) -> None:
        """Small bookkeeping changes (leaving, trial). Bumps rev, so a check in progress doesn't save over them."""
        bad = set(fields) - set(POSITION_COLUMNS)
        if bad:
            raise ValueError(f"Unknown columns {bad}")
        now = int(time.time())
        with self._conn() as conn:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE {self.T_POS} SET {sets}, updated_at=?, rev=COALESCE(rev, 0)+1 WHERE slug=?",
                         (*fields.values(), now, slug))
            if event:
                conn.execute(f"INSERT INTO {self.T_EVENTS} (ts, slug, kind, message) VALUES (?, ?, ?, ?)", (now, slug, *event))
            conn.commit()

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    @staticmethod
    def _row(r) -> Dict[str, Any]:
        return dict(zip(POSITION_COLUMNS, r))

    def get_positions(self, include_removed: bool = False) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {', '.join(POSITION_COLUMNS)} FROM {self.T_POS} "
                                + ("" if include_removed else "WHERE status != 'removed' ")
                                + "ORDER BY created_at, slug").fetchall()
        return [self._row(r) for r in rows]

    def get_position(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {', '.join(POSITION_COLUMNS)} FROM {self.T_POS} WHERE slug=?", (slug,)).fetchone()
        return self._row(r) if r else None

    def add_position(self, slug: str, note: str = "Added. The first offer goes in at the next check.",
                     source: str = "manual") -> bool:
        """Starts simulating a collection. False if it is already there (a removed one is brought back)."""
        existing = self.get_position(slug)
        if existing is not None:
            if existing["status"] != "removed":
                return False
            self.set_status(slug, "start")
            return True
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(f"INSERT INTO {self.T_POS} (slug, name, status, round, rev, times_outbid, note, "
                         "created_at, updated_at, source, auto_stopped) VALUES (?, ?, 'bidding', 1, 0, 0, ?, ?, ?, ?, 0)",
                         (slug, slug, note, now, now, source))
            conn.execute(f"INSERT INTO {self.T_EVENTS} (ts, slug, kind, message) VALUES (?, ?, 'added', ?)", (now, slug, note))
            conn.commit()
        return True

    def set_status(self, slug: str, action: str, closing_trade: Optional[Dict[str, Any]] = None,
                   note: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Dashboard buttons: stop, start or remove. Start resumes the listing if an item was bought.
        closing_trade: on remove, the bought item valued at today's floor, so it stays in the results.
        """
        pos = self.get_position(slug)
        if pos is None:
            return None
        now = int(time.time())
        if action == "stop":
            status, msg = "stopped", "Stopped from the dashboard."
        elif action == "remove":
            status, msg = "removed", "Removed from the dashboard."
        elif action == "start":
            if pos["entry_price"] is not None:
                status, msg = "holding", "Started again: still holding the item bought."
            else:
                status, msg = "bidding", "Started again: placing a new offer at the next check."
        else:
            raise ValueError(f"Unknown action {action}")
        with self._conn() as conn:
            note = note or msg
            fields = {"status": status, "note": None, "updated_at": now, "auto_stopped": 0, "leaving": 0}
            if action == "start" and status == "bidding":
                # A fresh offer: sales from while it was stopped don't count
                fields.update({"bid": None, "bid_state": None, "round_started_at": None, "last_sale_check": None,
                               "capped_since": None})
            if action == "start" and status == "holding":
                fields.update({"listed_at": None, "ask": None, "last_sale_check": None})
            if action == "remove" and closing_trade:
                fields.update({"entry_price": None, "entry_at": None, "entry_tx": None, "entry_token": None,
                               "ask": None, "listed_at": None, "entry_metrics": None})
                keys = list(closing_trade.keys())
                conn.execute(f"INSERT INTO {self.T_TRADES} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                             [closing_trade[k] for k in keys])
            sets = ", ".join(f"{k}=?" for k in fields)
            # rev+1 in SQL: a check the bot started before this click can't save over it
            conn.execute(f"UPDATE {self.T_POS} SET {sets}, rev=COALESCE(rev, 0)+1 WHERE slug=?", (*fields.values(), slug))
            conn.execute(f"INSERT INTO {self.T_EVENTS} (ts, slug, kind, message) VALUES (?, ?, ?, ?)",
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
            cur = conn.execute(f"UPDATE {self.T_POS} SET {', '.join(f'{c}=?' for c in cols)} "
                               f"WHERE slug=? AND rev=?", (*[pos.get(c) for c in cols], pos["slug"], expected_rev))
            if cur.rowcount != 1:
                conn.rollback()
                return False
            conn.executemany(f"INSERT INTO {self.T_EVENTS} (ts, slug, kind, message) VALUES (?, ?, ?, ?)", list(events))
            conn.executemany(f"INSERT OR REPLACE INTO {self.T_TICKS} (slug, ts, round, phase, bid, competitor_top, floor, ask) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", list(ticks))
            for t in trades:
                keys = list(t.keys())
                conn.execute(f"INSERT INTO {self.T_TRADES} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                             [t[k] for k in keys])
            conn.executemany(f"INSERT OR IGNORE INTO {self.T_SEEN} (slug, event_id, ts) VALUES (?, ?, ?)", list(seen))
            # Housekeeping: keep 30 days of checks and 3 days of judged sales
            conn.execute(f"DELETE FROM {self.T_TICKS} WHERE ts < ?", (now - 30 * 86400,))
            conn.execute(f"DELETE FROM {self.T_SEEN} WHERE ts < ?", (now - 3 * 86400,))
            conn.commit()
        return True

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def get_ticks(self, slug: str, round_no: Optional[int] = None, since_ts: int = 0) -> List[Dict[str, Any]]:
        q = f"SELECT ts, round, phase, bid, competitor_top, floor, ask FROM {self.T_TICKS} WHERE slug=? AND ts>=?"
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
                    f"SELECT event_id FROM {self.T_SEEN} WHERE slug=? AND event_id IN ({','.join('?' * len(part))})",
                    [slug, *part]))
        return out

    def get_round_sales(self, slug: str, from_round: int) -> List[Dict[str, Any]]:
        """Completed sales (not removals) of a collection from a round on, oldest first."""
        with self._conn() as conn:
            rows = conn.execute(f"SELECT round, net, currency FROM {self.T_TRADES} WHERE slug=? AND round>=? AND how='sold' "
                                "ORDER BY id", (slug, from_round)).fetchall()
        return [dict(zip(("round", "net", "currency"), r)) for r in rows]

    def get_trades(self, limit: int = 200) -> List[Dict[str, Any]]:
        cols = ("id", "slug", "round", "currency", "entry_price", "entry_at", "entry_tx", "entry_token",
                "exit_price", "exit_at", "exit_tx", "fees", "gas", "net", "roi_pct", "times_outbid", "how",
                "entry_metrics")
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {', '.join(cols)} FROM {self.T_TRADES} ORDER BY exit_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def get_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT ts, slug, kind, message FROM {self.T_EVENTS} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "slug": r[1], "kind": r[2], "message": r[3]} for r in rows]

    def get_last_events(self) -> Dict[str, Dict[str, Any]]:
        """Each collection's latest event (problems aside), however busy the others are."""
        with self._conn() as conn:
            rows = conn.execute(f"SELECT ts, slug, kind, message FROM {self.T_EVENTS} WHERE id IN "
                                f"(SELECT MAX(id) FROM {self.T_EVENTS} WHERE kind != 'problem' GROUP BY slug)").fetchall()
        return {r[1]: {"ts": r[0], "slug": r[1], "kind": r[2], "message": r[3]} for r in rows}

    def log(self, slug: str, kind: str, message: str):
        with self._conn() as conn:
            conn.execute(f"INSERT INTO {self.T_EVENTS} (ts, slug, kind, message) VALUES (?, ?, ?, ?)",
                         (int(time.time()), slug, kind, message))
            conn.commit()

