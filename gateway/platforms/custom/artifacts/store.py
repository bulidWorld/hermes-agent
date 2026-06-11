"""SQLite-backed run artifact metadata store."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS run_artifacts (
    artifact_id  TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    session_id   TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT NOT NULL DEFAULT '',
    tool_name    TEXT NOT NULL DEFAULT '',
    public_id    TEXT NOT NULL,
    filename     TEXT NOT NULL,
    mime_type    TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    remote_url   TEXT NOT NULL,
    remote_path  TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_artifacts_run_id
    ON run_artifacts(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_artifacts_session_id
    ON run_artifacts(session_id, created_at);
"""


class RunArtifactStore:
    """Persist file-server artifacts produced by ``/v1/runs``."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        try:
            from hermes_constants import get_hermes_home

            home = get_hermes_home()
        except Exception:
            home = Path.home() / ".hermes"

        if db_path is None:
            db_path = str(home / "run_artifacts.db")

        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            logger.warning("RunArtifactStore: falling back to in-memory SQLite")
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)

        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(self._conn, db_label="run_artifacts.db")
        self._conn.executescript(SCHEMA_SQL)
        self._ensure_columns()
        self._conn.commit()

    def put(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        artifact_id: str,
        public_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        remote_url: str,
        remote_path: str,
    ) -> Dict[str, Any]:
        now = time.time()
        self._conn.execute(
            """INSERT OR REPLACE INTO run_artifacts
               (artifact_id, run_id, session_id, tool_call_id, tool_name,
                public_id, filename, mime_type,
                size_bytes, remote_url, remote_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id,
                run_id,
                session_id,
                tool_call_id,
                tool_name,
                public_id,
                filename,
                mime_type,
                int(size_bytes),
                remote_url,
                remote_path,
                now,
            ),
        )
        self._conn.commit()
        meta = self.get(run_id, artifact_id)
        return meta or {}

    def get(self, run_id: str, artifact_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT artifact_id, run_id, session_id, tool_call_id, tool_name, "
            "public_id, filename, mime_type, "
            "size_bytes, remote_url, remote_path, created_at "
            "FROM run_artifacts WHERE run_id = ? AND artifact_id = ?",
            (run_id, artifact_id),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_run(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT artifact_id, run_id, session_id, tool_call_id, tool_name, "
            "public_id, filename, mime_type, "
            "size_bytes, remote_url, remote_path, created_at "
            "FROM run_artifacts WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_tool_call_ids(self, tool_call_ids: List[str]) -> List[Dict[str, Any]]:
        ids = sorted({str(item) for item in tool_call_ids if item})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            "SELECT artifact_id, run_id, session_id, tool_call_id, tool_name, "
            "public_id, filename, mime_type, "
            "size_bytes, remote_url, remote_path, created_at "
            f"FROM run_artifacts WHERE tool_call_id IN ({placeholders}) "
            "ORDER BY created_at ASC",
            tuple(ids),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def public_metadata(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": item["artifact_id"],
            "artifact_id": item["artifact_id"],
            "run_id": item["run_id"],
            "session_id": item["session_id"],
            "tool_call_id": item["tool_call_id"],
            "tool_name": item["tool_name"],
            "file_id": item["public_id"],
            "filename": item["filename"],
            "mime_type": item["mime_type"],
            "size": item["size_bytes"],
            "created_at": item["created_at"],
        }

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _ensure_columns(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(run_artifacts)").fetchall()
        columns = {row[1] for row in rows}
        if "session_id" not in columns:
            self._conn.execute(
                "ALTER TABLE run_artifacts "
                "ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
            )
        if "tool_call_id" not in columns:
            self._conn.execute(
                "ALTER TABLE run_artifacts "
                "ADD COLUMN tool_call_id TEXT NOT NULL DEFAULT ''"
            )
        if "tool_name" not in columns:
            self._conn.execute(
                "ALTER TABLE run_artifacts "
                "ADD COLUMN tool_name TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        return {
            "artifact_id": row[0],
            "run_id": row[1],
            "session_id": row[2],
            "tool_call_id": row[3],
            "tool_name": row[4],
            "public_id": row[5],
            "filename": row[6],
            "mime_type": row[7],
            "size_bytes": row[8],
            "remote_url": row[9],
            "remote_path": row[10],
            "created_at": row[11],
        }
