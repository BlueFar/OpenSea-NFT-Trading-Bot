import sqlite3
import os
import json
from typing import Optional, Dict, Any
from datetime import datetime, timezone

class StateStore:
    """Persistent SQLite-backed state store for discovery checkpoints and candidate deduplication."""

    def __init__(self, db_path: str = "./state/bot.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10.0)

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
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
            conn.commit()

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
        """Checks if this candidate was already processed/recorded for the given calendar day."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM candidate_history WHERE slug=? AND date_str=?", (slug, date_str))
            return c.fetchone() is not None

    def record_candidate(self, slug: str, date_str: str, is_pass: bool, reasons: str = ""):
        """Records a candidate evaluation for the given calendar day."""
        with self._get_connection() as conn:
            c = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            c.execute("""
                INSERT INTO candidate_history (slug, date_str, is_pass, reasons, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(slug, date_str) DO UPDATE SET
                    is_pass=excluded.is_pass,
                    reasons=excluded.reasons,
                    created_at=excluded.created_at
            """, (slug, date_str, 1 if is_pass else 0, reasons, now))
            conn.commit()

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

            c.execute("SELECT key, value, updated_at FROM bot_telemetry")
            telemetry = {row[0]: {"value": row[1], "updated_at": row[2]} for row in c.fetchall()}

            c.execute("SELECT source, cursor, updated_at FROM discovery_checkpoints")
            checkpoints = {row[0]: {"cursor": row[1], "updated_at": row[2]} for row in c.fetchall()}

            return {
                "total_candidates_found": total_candidates,
                "checkpoints": checkpoints,
                "telemetry": telemetry,
            }
