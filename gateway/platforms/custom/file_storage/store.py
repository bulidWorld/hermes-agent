"""
Per-instance file metadata store with a local on-disk cache.

File *bodies* live on the file-storage service.  This module keeps a
lightweight SQLite table of metadata (name, MIME type, size, remote URL,
TTL, and the service-assigned *public_id*) and manages a local cache
directory so that agent tools can access previously-uploaded files by path.

Independent of :mod:`gateway.platforms.custom.file_store` — uses its own
database file (``file_storage_store.db``) and adds the ``public_id`` column.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import FileStorageServiceClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL schema (module-level constant — follow hermes_state.py pattern)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    file_id     TEXT PRIMARY KEY,
    public_id   TEXT NOT NULL,
    filename    TEXT NOT NULL,
    mime_type   TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    remote_url  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    accessed_at REAL NOT NULL,
    expires_at  REAL NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_FILES = 1000
_DEFAULT_FILE_TTL = 86400  # 24 hours
_DEFAULT_CACHE_TTL = 3600  # 1 hour

_FILE_ID_PREFIX = "file_"


def _generate_file_id() -> str:
    return _FILE_ID_PREFIX + uuid.uuid4().hex[:28]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class FileStorageStore:
    """SQLite-backed file metadata store with an LRU local cache.

    Parameters
    ----------
    client:
        The file-storage service client used for download / delete.
    db_path:
        Path to the SQLite metadata database.  Defaults to
        ``<hermes-home>/file_storage_store.db``.
    cache_dir:
        Local cache directory for downloaded files.  Defaults to
        ``<hermes-home>/file_storage_cache``.
    max_files:
        Maximum number of file metadata records to retain.
    default_ttl:
        Default time-to-live in seconds for newly stored files.
    cache_ttl:
        How long (seconds) a locally cached file stays valid before
        being re-downloaded.
    """

    def __init__(
        self,
        client: FileStorageServiceClient,
        db_path: Optional[str] = None,
        cache_dir: Optional[str] = None,
        max_files: int = _DEFAULT_MAX_FILES,
        default_ttl: int = _DEFAULT_FILE_TTL,
        cache_ttl: int = _DEFAULT_CACHE_TTL,
    ) -> None:
        self._client = client
        self._max_files = max_files
        self._default_ttl = default_ttl
        self._cache_ttl = cache_ttl

        # Resolve home
        try:
            from hermes_constants import get_hermes_home

            _home = get_hermes_home()
        except Exception:
            _home = Path.home() / ".hermes"

        if db_path is None:
            db_path = str(_home / "file_storage_store.db")
        if cache_dir is None:
            cache_dir = str(_home / "file_storage_cache")

        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # SQLite
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)

        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(self._conn, db_label="file_storage_store.db")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Metadata CRUD
    # ------------------------------------------------------------------

    def put(
        self,
        file_id: str,
        public_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        remote_url: str,
        ttl_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Insert a metadata record (the file body was already uploaded)."""
        now = time.time()
        expires_at = now + (
            ttl_seconds if ttl_seconds is not None else self._default_ttl
        )
        self._conn.execute(
            """INSERT OR REPLACE INTO files
               (file_id, public_id, filename, mime_type, size_bytes,
                remote_url, created_at, accessed_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file_id,
                public_id,
                filename,
                mime_type,
                size_bytes,
                remote_url,
                now,
                now,
                expires_at,
            ),
        )
        self._conn.commit()
        self._evict_if_needed()
        return self.get(file_id)

    def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Return metadata dict for *file_id*, or ``None``."""
        row = self._conn.execute(
            "SELECT file_id, public_id, filename, mime_type, size_bytes, "
            "remote_url, created_at, expires_at FROM files WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        # Touch access time
        self._conn.execute(
            "UPDATE files SET accessed_at = ? WHERE file_id = ?",
            (time.time(), file_id),
        )
        self._conn.commit()
        return {
            "file_id": row[0],
            "public_id": row[1],
            "filename": row[2],
            "mime_type": row[3],
            "size_bytes": row[4],
            "remote_url": row[5],
            "created_at": row[6],
            "expires_at": row[7],
        }

    def delete(self, file_id: str) -> bool:
        """Delete metadata record.  Does NOT touch remote or cache."""
        cursor = self._conn.execute(
            "DELETE FROM files WHERE file_id = ?", (file_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_all(self) -> List[Dict[str, Any]]:
        """Return all metadata records, newest first."""
        rows = self._conn.execute(
            "SELECT file_id, public_id, filename, mime_type, size_bytes, "
            "remote_url, created_at, expires_at FROM files "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "file_id": r[0],
                "public_id": r[1],
                "filename": r[2],
                "mime_type": r[3],
                "size_bytes": r[4],
                "remote_url": r[5],
                "created_at": r[6],
                "expires_at": r[7],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Local cache
    # ------------------------------------------------------------------

    def get_local_path(self, file_id: str) -> Path:
        """Return the expected local-cache path for *file_id*."""
        return self._cache_dir / file_id

    def is_cached(self, file_id: str) -> bool:
        """Return ``True`` when a valid (non-expired) local copy exists."""
        path = self.get_local_path(file_id)
        if not path.is_file():
            return False
        mtime = path.stat().st_mtime
        return (time.time() - mtime) < self._cache_ttl

    async def ensure_local(self, file_id: str, request=None) -> Optional[Path]:
        """Make sure *file_id* is present on the local filesystem.

        1. Cache hit → return path immediately.
        2. Cache miss → download from file-storage service via *public_id*.
        3. Download fails → return ``None``.
        """
        path = self.get_local_path(file_id)
        if self.is_cached(file_id):
            return path

        meta = self.get(file_id)
        if meta is None:
            logger.warning("ensure_local: no metadata for %s", file_id)
            return None

        remote_id = meta["public_id"]
        data = await self._client.download(remote_id, request=request)
        if data is None:
            logger.warning("ensure_local: download failed for %s", file_id)
            return None

        try:
            path.write_bytes(data)
        except OSError as exc:
            logger.error(
                "ensure_local: cannot write cache for %s: %s", file_id, exc,
            )
            return None

        return path

    async def read_content(self, file_id: str, request=None) -> Optional[bytes]:
        """Return file bytes, going through the local cache."""
        path = await self.ensure_local(file_id, request=request)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError as exc:
            logger.warning("read_content: cannot read %s: %s", file_id, exc)
            return None

    # ------------------------------------------------------------------
    # Eviction & sweep
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Evict oldest-by-access records when over capacity."""
        row = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()
        count = row[0] if row else 0
        if count <= self._max_files:
            return

        excess = count - self._max_files
        evict_rows = self._conn.execute(
            "SELECT file_id FROM files ORDER BY accessed_at ASC LIMIT ?",
            (excess,),
        ).fetchall()
        evict_ids = [r[0] for r in evict_rows]
        if evict_ids:
            placeholders = ",".join("?" for _ in evict_ids)
            self._conn.execute(
                f"DELETE FROM files WHERE file_id IN ({placeholders})",
                evict_ids,
            )
            self._conn.commit()
            for fid in evict_ids:
                self._remove_cache_file(fid)

    def _remove_cache_file(self, file_id: str) -> None:
        path = self.get_local_path(file_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    async def sweep_expired(self, request=None) -> int:
        """Delete expired metadata + cached files + remote files.

        Returns the number of records cleaned up.
        """
        now = time.time()
        expired = self._conn.execute(
            "SELECT file_id, public_id FROM files WHERE expires_at < ?",
            (now,),
        ).fetchall()
        if not expired:
            return 0

        for fid, pub_id in expired:
            try:
                await self._client.delete(pub_id, request=request)
            except Exception:
                pass
            self._remove_cache_file(fid)

        expired_ids = [r[0] for r in expired]
        placeholders = ",".join("?" for _ in expired_ids)
        self._conn.execute(
            f"DELETE FROM files WHERE file_id IN ({placeholders})",
            expired_ids,
        )
        self._conn.commit()
        logger.info("sweep_expired: removed %d file(s)", len(expired_ids))
        return len(expired_ids)

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()
        return row[0] if row else 0
