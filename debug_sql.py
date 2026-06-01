"""Optional SQL debug logging for sqlite3 connections.

When the environment variable ``HERMES_DEBUG_SQL=1`` is set, every
``conn.execute()`` and ``conn.cursor()`` call is intercepted and the
SQL text, parameters, and result row counts are logged at DEBUG level
to the module's logger (routed to agent.log).

Usage::

    import sqlite3
    from debug_sql import maybe_wrap_connection

    conn = sqlite3.connect("mydb.db")
    conn = maybe_wrap_connection(conn)  # no-op unless HERMES_DEBUG_SQL is set

The wrapping is idempotent — calling it multiple times on the same
connection is safe.
"""

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

_ENV_VAR = "HERMES_DEBUG_SQL"

_SQL_DEBUG = os.environ.get(_ENV_VAR, "").strip().lower() in (
    "1", "true", "yes", "on",
)


class _DebugCursorProxy:
    """Wraps a sqlite3 Cursor to log queries and their results.

    Intercepts ``fetchone()`` and ``fetchall()`` so the caller's existing
    code needs zero changes — the proxy is returned transparently from
    the wrapped ``conn.execute()``.
    """

    __slots__ = ("_cursor", "_sql", "_params", "_log_prefix")

    def __init__(self, cursor: sqlite3.Cursor, sql: str, params) -> None:
        self._cursor = cursor
        self._sql = sql
        self._params = params
        if len(sql) <= 500:
            self._log_prefix = f"SQL: {sql}"
        else:
            self._log_prefix = f"SQL: {sql[:500]}..."

    def fetchone(self):
        row = self._cursor.fetchone()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "%s | params=%s | => %s", self._log_prefix, self._params, row,
            )
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "%s | params=%s | => %d rows",
                self._log_prefix,
                self._params,
                len(rows),
            )
        return rows

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _DebugConnectionWrapper:
    """Transparent proxy around ``sqlite3.Connection`` for SQL debug logging.

    sqlite3.Connection is a C extension — its instance attributes can't be
    replaced.  This pure-Python wrapper intercepts ``execute()`` and
    ``cursor()`` to return ``_DebugCursorProxy`` instances, while delegating
    every other attribute (including ``commit``, ``rollback``, ``close``,
    ``row_factory``, etc.) to the real connection.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)

    def execute(self, sql: str, parameters=None):
        conn = self._conn
        cursor = conn.execute(sql, parameters) if parameters is not None else conn.execute(sql)
        return _DebugCursorProxy(cursor, sql, parameters)

    def cursor(self, factory=None):
        conn = self._conn
        cursor = conn.cursor(factory) if factory is not None else conn.cursor()
        return _DebugCursorProxy(cursor, "<<cursor()>>", None)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value) -> None:
        # During __init__, _conn is set via object.__setattr__ before the
        # instance __dict__ exists.  After that, delegate to the real conn.
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)


def maybe_wrap_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Return a debug-wrapped connection if ``HERMES_DEBUG_SQL`` is set.

    When the env var is **not** set, returns *conn* unchanged — zero overhead.
    Idempotent: if *conn* is already wrapped, returns it as-is.
    """
    if not _SQL_DEBUG:
        return conn

    if isinstance(conn, _DebugConnectionWrapper):
        return conn

    logger.debug("SQL debug logging enabled (HERMES_DEBUG_SQL=%s)", os.environ.get(_ENV_VAR))
    return _DebugConnectionWrapper(conn)
