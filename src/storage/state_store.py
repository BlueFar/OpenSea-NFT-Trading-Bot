import sqlite3
import os
import json
from typing import Optional, Dict, Any, List, Iterable
from datetime import datetime, timezone

class StateStore:
    """Persistent SQLite-backed state store for discovery checkpoints and candidate deduplication."""

    def __init__(self, db_path: str = "./state/bot.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        # FULL sync: a committed write survives a sudden power cut (WAL keeps this cheap)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Write-ahead log: no torn database file if the power goes mid-write, and readers
            # (the dashboard) never block the bot. The mode is stored in the database file.
            cursor.execute("PRAGMA journal_mode=WAL")
            # Table for discovery pagination cursors
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS discovery_checkpoints (
                    source TEXT PRIMARY KEY,
                    cursor TEXT,
                    updated_at TIMESTAMP
                )
            """)
            # Table for deduplicating candidates per calendar day
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS candidate_history (
                    slug TEXT,
                    date_str TEXT,
                    is_pass INTEGER,
                    reasons TEXT,
                    created_at TIMESTAMP,
                    PRIMARY KEY (slug, date_str)
                )
            """)
            # Table for operational health and metrics
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_telemetry (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP
                )
            """)
            # Table for the universe of monitored collections
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS monitored_collections (
                    slug TEXT PRIMARY KEY,
                    discovery_source TEXT,
                    last_evaluated_at TIMESTAMP,
                    evaluation_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP
                )
            """)
            # Bot-recorded floor price history (OpenSea v2 has no public floor time-series endpoint)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS floor_snapshots (
                    slug TEXT,
                    ts INTEGER,
                    floor_price REAL,
                    currency TEXT,
                    PRIMARY KEY (slug, ts)
                )
            """)
            # Columns added after the initial schema; migrate existing databases in place
            self._add_column_if_missing(cursor, "monitored_collections", "shortlisted", "INTEGER DEFAULT 0")
            self._add_column_if_missing(cursor, "candidate_history", "reject_filter", "TEXT")
            self._add_column_if_missing(cursor, "candidate_history", "details", "TEXT")
            self._add_column_if_missing(cursor, "monitored_collections", "chain", "TEXT")
            self._add_column_if_missing(cursor, "monitored_collections", "safelist_status", "TEXT")
            # Plain-language activity shown on the dashboard (bot started, internet lost, ...)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TIMESTAMP,
                    kind TEXT,
                    message TEXT
                )
            """)
            # Dollar price per coin, from collections' OpenSea payment tokens (for the dashboard)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS usd_prices (
                    symbol TEXT PRIMARY KEY,
                    usd REAL,
                    updated_at TEXT
                )
            """)
            conn.commit()

    @staticmethod
    def _add_column_if_missing(cursor: sqlite3.Cursor, table: str, column: str, decl: str):
        cursor.execute(f"PRAGMA table_info({table})")
        if column not in {row[1] for row in cursor.fetchall()}:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def record_floor_snapshot(self, slug: str, ts: int, floor_price: float, currency: str = "ETH", retention_days: int = 10):
        """Stores the current floor price so 1-day / 7-day changes can be computed later."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO floor_snapshots (slug, ts, floor_price, currency)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(slug, ts) DO UPDATE SET floor_price=excluded.floor_price, currency=excluded.currency
            """, (slug, int(ts), float(floor_price), currency))
            c.execute("DELETE FROM floor_snapshots WHERE slug=? AND ts < ?", (slug, int(ts) - retention_days * 86400))
            conn.commit()

    def get_floor_snapshot_near(self, slug: str, target_ts: int, tolerance_seconds: float) -> Optional[Dict[str, Any]]:
        """Returns the snapshot closest to target_ts within +/- tolerance_seconds, or None."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT ts, floor_price, currency FROM floor_snapshots
                WHERE slug=? AND ts BETWEEN ? AND ?
                ORDER BY ABS(ts - ?) ASC
                LIMIT 1
            """, (slug, int(target_ts - tolerance_seconds), int(target_ts + tolerance_seconds), int(target_ts)))
            row = c.fetchone()
            if not row:
                return None
            return {"ts": row[0], "floor_price": row[1], "currency": row[2]}

    def get_floor_snapshots(self, slug: str, from_ts: int, to_ts: int) -> List[Dict[str, Any]]:
        """All snapshots for a collection between two times, oldest first."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT ts, floor_price, currency FROM floor_snapshots
                WHERE slug=? AND ts BETWEEN ? AND ? ORDER BY ts ASC
            """, (slug, int(from_ts), int(to_ts))).fetchall()
        return [{"ts": r[0], "floor_price": r[1], "currency": r[2]} for r in rows]

    def set_shortlisted(self, slug: str, shortlisted: bool):
        """Marks whether a collection passed the cheap structural filters (age, verification, listings, frequency)."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE monitored_collections SET shortlisted=? WHERE slug=?", (1 if shortlisted else 0, slug))
            conn.commit()

    def add_discovered_slugs(self, slugs: List[str], source: str = "discovery",
                             meta: Optional[Dict[str, Dict[str, Optional[str]]]] = None):
        """
        Registers newly discovered collection slugs into the monitored universe.
        meta maps slug -> {"chain", "safelist_status"} as seen in the discovery listing.
        """
        if not slugs:
            return
        meta = meta or {}
        with self._get_connection() as conn:
            c = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            for s in slugs:
                m = meta.get(s) or {}
                c.execute("""
                    INSERT INTO monitored_collections
                        (slug, discovery_source, last_evaluated_at, evaluation_count, created_at, chain, safelist_status)
                    VALUES (?, ?, NULL, 0, ?, ?, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        chain=COALESCE(excluded.chain, monitored_collections.chain),
                        safelist_status=COALESCE(excluded.safelist_status, monitored_collections.safelist_status)
                """, (s, source, now, m.get("chain"), m.get("safelist_status")))
            conn.commit()

    def set_collection_info(self, slug: str, chain: Optional[str] = None, safelist_status: Optional[str] = None):
        """Stores chain / verification seen during a full evaluation."""
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE monitored_collections
                SET chain=COALESCE(?, chain), safelist_status=COALESCE(?, safelist_status)
                WHERE slug=?
            """, (chain, safelist_status, slug))
            conn.commit()

    def get_collections_due_for_evaluation(
        self,
        limit: int = 20,
        shortlist_refresh_seconds: Optional[float] = None,
        chains: Optional[Iterable[str]] = None,
        allowed_statuses: Optional[Iterable[str]] = None,
    ) -> List[str]:
        """
        Retrieves collection slugs due for evaluation.
        If shortlist_refresh_seconds is given, up to half the slots go to shortlisted collections
        not evaluated within that window, so their floor history keeps building.
        The rest prioritizes collections never evaluated (NULL), then those evaluated longest ago,
        taking turns between chains so one busy chain cannot starve the others.
        chains: only collections on these chains (or with an unknown chain).
        allowed_statuses: skip collections whose discovery listing showed another safelist status.
        """
        where, params = ["1=1"], []
        if chains is not None:
            chains = list(chains)
            where.append(f"(chain IS NULL OR chain IN ({','.join('?' * len(chains))}))" if chains else "chain IS NULL")
            params.extend(chains)
        if allowed_statuses is not None:
            statuses = list(allowed_statuses)
            where.append(f"(safelist_status IS NULL OR safelist_status IN ({','.join('?' * len(statuses))}))" if statuses else "safelist_status IS NULL")
            params.extend(statuses)
        base_where = " AND ".join(where)

        with self._get_connection() as conn:
            c = conn.cursor()
            selected: List[str] = []
            if shortlist_refresh_seconds is not None:
                cutoff = datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() - shortlist_refresh_seconds, tz=timezone.utc
                ).isoformat()
                c.execute(f"""
                    SELECT slug FROM monitored_collections
                    WHERE {base_where} AND shortlisted=1 AND last_evaluated_at IS NOT NULL AND last_evaluated_at < ?
                    ORDER BY last_evaluated_at ASC
                    LIMIT ?
                """, (*params, cutoff, max(1, limit // 2)))
                selected = [r[0] for r in c.fetchall()]

            c.execute(f"""
                SELECT slug FROM (
                    SELECT slug, last_evaluated_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY COALESCE(chain, '')
                            ORDER BY CASE WHEN last_evaluated_at IS NULL THEN 0 ELSE 1 END, last_evaluated_at ASC
                        ) AS turn
                    FROM monitored_collections
                    WHERE {base_where} AND slug NOT IN ({",".join("?" * len(selected))})
                )
                ORDER BY turn ASC,
                    CASE WHEN last_evaluated_at IS NULL THEN 0 ELSE 1 END,
                    last_evaluated_at ASC
                LIMIT ?
            """, (*params, *selected, limit - len(selected)))
            selected.extend(r[0] for r in c.fetchall())
            return selected

    def mark_collection_evaluated(self, slug: str):
        """Updates last_evaluated_at and increments evaluation_count for a collection."""
        with self._get_connection() as conn:
            c = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            c.execute("""
                UPDATE monitored_collections
                SET last_evaluated_at = ?, evaluation_count = evaluation_count + 1
                WHERE slug = ?
            """, (now, slug))
            conn.commit()

    def get_monitored_collection_count(self) -> int:
        """Returns total count of collections in the monitored universe."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM monitored_collections")
            return c.fetchone()[0]


    def save_checkpoint(self, source: str, cursor_val: Optional[str]):
        """Persists a discovery cursor checkpoint."""
        with self._get_connection() as conn:
            c = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            c.execute("""
                INSERT INTO discovery_checkpoints (source, cursor, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at
            """, (source, cursor_val, now))
            conn.commit()

    def get_checkpoint(self, source: str) -> Optional[str]:
        """Retrieves the last saved cursor checkpoint for a discovery source."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT cursor FROM discovery_checkpoints WHERE source=?", (source,))
            row = c.fetchone()
            return row[0] if row else None

    def is_candidate_recorded_today(self, slug: str, date_str: str) -> bool:
        """Checks if this candidate was already processed/recorded for the given calendar day as a passing candidate."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM candidate_history WHERE slug=? AND date_str=? AND is_pass=1", (slug, date_str))
            return c.fetchone() is not None

    def record_candidate(self, slug: str, date_str: str, is_pass: bool, reasons: str = "",
                         reject_filter: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        """
        Records a candidate evaluation for the given calendar day.
        A later failure on the same day never overwrites a recorded pass (its Info.md already exists).
        details: JSON-serialisable facts for the dashboard (rule value vs limit, trade plan, ...).
        """
        with self._get_connection() as conn:
            c = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            c.execute("""
                INSERT INTO candidate_history (slug, date_str, is_pass, reasons, created_at, reject_filter, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug, date_str) DO UPDATE SET
                    is_pass=excluded.is_pass,
                    reasons=excluded.reasons,
                    created_at=excluded.created_at,
                    reject_filter=excluded.reject_filter,
                    details=excluded.details
                WHERE candidate_history.is_pass = 0 OR excluded.is_pass = 1
            """, (slug, date_str, 1 if is_pass else 0, reasons, now, None if is_pass else reject_filter,
                  json.dumps(details, default=str) if details is not None else None))
            conn.commit()

    # ------------------------------------------------------------------
    # Dashboard queries
    # ------------------------------------------------------------------
    def log_event(self, kind: str, message: str):
        """Adds a plain-language activity line (kept for 30 days)."""
        with self._get_connection() as conn:
            now = datetime.now(timezone.utc)
            conn.execute("INSERT INTO bot_events (ts, kind, message) VALUES (?, ?, ?)", (now.isoformat(), kind, message))
            cutoff = datetime.fromtimestamp(now.timestamp() - 30 * 86400, tz=timezone.utc).isoformat()
            conn.execute("DELETE FROM bot_events WHERE ts < ?", (cutoff,))
            conn.commit()

    def get_recent_events(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT ts, kind, message FROM bot_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{"ts": r[0], "kind": r[1], "message": r[2]} for r in rows]

    @staticmethod
    def _row_to_result(r) -> Dict[str, Any]:
        try:
            details = json.loads(r[6]) if r[6] else {}
        except (TypeError, ValueError):
            details = {}
        return {"slug": r[0], "date_str": r[1], "is_pass": bool(r[2]), "reasons": r[3],
                "created_at": r[4], "reject_filter": r[5], "details": details}

    _RESULT_COLS = "slug, date_str, is_pass, reasons, created_at, reject_filter, details"

    def get_results_since(self, since_date_str: str, passes_only: bool = False, limit: int = 2000) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(f"""
                SELECT {self._RESULT_COLS} FROM candidate_history
                WHERE date_str >= ? {"AND is_pass=1" if passes_only else ""}
                ORDER BY created_at DESC LIMIT ?
            """, (since_date_str, limit)).fetchall()
            return [self._row_to_result(r) for r in rows]

    def get_result(self, slug: str, date_str: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            if date_str:
                r = conn.execute(f"SELECT {self._RESULT_COLS} FROM candidate_history WHERE slug=? AND date_str=?",
                                 (slug, date_str)).fetchone()
            else:
                r = conn.execute(f"SELECT {self._RESULT_COLS} FROM candidate_history WHERE slug=? ORDER BY created_at DESC LIMIT 1",
                                 (slug,)).fetchone()
            return self._row_to_result(r) if r else None

    def count_results_on(self, date_str: str) -> int:
        with self._get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM candidate_history WHERE date_str=?", (date_str,)).fetchone()[0]

    def count_shortlisted(self) -> int:
        with self._get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM monitored_collections WHERE shortlisted=1").fetchone()[0]

    def count_skipped_unverified(self, allowed_statuses: Iterable[str]) -> int:
        statuses = list(allowed_statuses)
        with self._get_connection() as conn:
            q = "SELECT COUNT(*) FROM monitored_collections WHERE safelist_status IS NOT NULL"
            if statuses:
                q += f" AND safelist_status NOT IN ({','.join('?' * len(statuses))})"
            return conn.execute(q, statuses).fetchone()[0]

    def count_by_chain(self) -> Dict[str, int]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT COALESCE(chain, 'unknown'), COUNT(*) FROM monitored_collections GROUP BY 1").fetchall()
            return {r[0]: r[1] for r in rows}

    def get_collection_chain(self, slug: str) -> Optional[str]:
        with self._get_connection() as conn:
            r = conn.execute("SELECT chain FROM monitored_collections WHERE slug=?", (slug,)).fetchone()
            return r[0] if r else None

    def get_oldest_floor_snapshot_ts(self) -> Optional[int]:
        with self._get_connection() as conn:
            r = conn.execute("SELECT MIN(ts) FROM floor_snapshots").fetchone()
            return r[0] if r and r[0] is not None else None

    def get_rejection_funnel(self, since_date_str: str) -> Dict[str, int]:
        """Counts evaluations since the given date grouped by the filter that rejected them ('PASS' for passes)."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT CASE WHEN is_pass=1 THEN 'PASS' ELSE COALESCE(reject_filter, 'unknown') END AS k, COUNT(*)
                FROM candidate_history
                WHERE date_str >= ?
                GROUP BY k
                ORDER BY COUNT(*) DESC
            """, (since_date_str,))
            return {row[0]: row[1] for row in c.fetchall()}

    def set_usd_prices(self, prices: Dict[str, float]):
        if not prices:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO usd_prices (symbol, usd, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET usd=excluded.usd, updated_at=excluded.updated_at
            """, [(sym, float(usd), now) for sym, usd in prices.items()])
            conn.commit()

    def get_usd_prices(self) -> Dict[str, Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT symbol, usd, updated_at FROM usd_prices").fetchall()
        return {r[0]: {"usd": r[1], "updated_at": r[2]} for r in rows}

    def update_telemetry(self, key: str, value: Any):
        """Updates a key-value telemetry item."""
        with self._get_connection() as conn:
            c = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            val_str = json.dumps(value) if not isinstance(value, str) else value
            c.execute("""
                INSERT INTO bot_telemetry (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (key, val_str, now))
            conn.commit()

    def get_telemetry(self, key: str) -> Optional[str]:
        """Retrieves a telemetry item."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM bot_telemetry WHERE key=?", (key,))
            row = c.fetchone()
            return row[0] if row else None

    def get_status_summary(self) -> Dict[str, Any]:
        """Returns overall health, counts, and status metrics."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM candidate_history WHERE is_pass=1")
            total_candidates = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM monitored_collections")
            monitored_count = c.fetchone()[0]

            c.execute("SELECT key, value, updated_at FROM bot_telemetry")
            telemetry = {row[0]: {"value": row[1], "updated_at": row[2]} for row in c.fetchall()}

            c.execute("SELECT source, cursor, updated_at FROM discovery_checkpoints")
            checkpoints = {row[0]: {"cursor": row[1], "updated_at": row[2]} for row in c.fetchall()}

            return {
                "total_candidates_found": total_candidates,
                "total_monitored_collections": monitored_count,
                "checkpoints": checkpoints,
                "telemetry": telemetry,
            }

    def get_all_candidates_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Returns candidate history records ordered by creation date descending."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT slug, date_str, is_pass, reasons, created_at
                FROM candidate_history
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
            rows = c.fetchall()
            return [
                {
                    "slug": r[0],
                    "date_str": r[1],
                    "is_pass": bool(r[2]),
                    "reasons": r[3],
                    "created_at": r[4],
                }
                for r in rows
            ]

    def get_all_monitored_collections(self, search: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Returns monitored collections with optional search query."""
        with self._get_connection() as conn:
            c = conn.cursor()
            if search:
                pattern = f"%{search.strip().lower()}%"
                c.execute("""
                    SELECT slug, discovery_source, last_evaluated_at, evaluation_count, created_at
                    FROM monitored_collections
                    WHERE LOWER(slug) LIKE ?
                    ORDER BY CASE WHEN last_evaluated_at IS NULL THEN 0 ELSE 1 END, last_evaluated_at DESC
                    LIMIT ?
                """, (pattern, limit))
            else:
                c.execute("""
                    SELECT slug, discovery_source, last_evaluated_at, evaluation_count, created_at
                    FROM monitored_collections
                    ORDER BY CASE WHEN last_evaluated_at IS NULL THEN 0 ELSE 1 END, last_evaluated_at DESC
                    LIMIT ?
                """, (limit,))
            rows = c.fetchall()
            return [
                {
                    "slug": r[0],
                    "discovery_source": r[1],
                    "last_evaluated_at": r[2],
                    "evaluation_count": r[3],
                    "created_at": r[4],
                }
                for r in rows
            ]
