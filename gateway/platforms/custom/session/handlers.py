"""
HTTP request handlers for ``/custom/v1/sessions/*`` endpoints.

These are extracted from :class:`APIServerAdapter` into standalone
classes with explicit dependencies injected via constructors.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_AuthChecker = Callable[..., Any]


def _openai_error(
    message: str,
    err_type: str = "invalid_request_error",
    code: Optional[str] = None,
) -> dict:
    return {"error": {"message": message, "type": err_type, "code": code}}


class CustomSessionHandlers:
    """Standalone handlers for custom session endpoints.

    Depends only on two injected callables — no reference to
    :class:`APIServerAdapter` needed.

    Parameters
    ----------
    auth_checker:
        Callable ``(request) -> Optional[Response]`` — returns ``None``
        when authentication succeeds.
    session_db_provider:
        Callable ``() -> Optional[SessionDB]`` — returns the shared
        session database instance, or ``None`` when unavailable.
    """

    def __init__(
        self,
        auth_checker: _AuthChecker,
        session_db_provider: Callable[[], Any],
    ) -> None:
        self._check_auth = auth_checker
        self._session_db_provider = session_db_provider

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def register_routes(self, app: Any) -> None:
        """Register custom session routes on the aiohttp *app*."""
        app.router.add_get(
            "/custom/v1/sessions", self.handle_list_sessions,
        )
        app.router.add_get(
            "/custom/v1/sessions/{session_id}/messages",
            self.handle_get_session_messages,
        )

    # ------------------------------------------------------------------
    # GET /custom/v1/sessions
    # ------------------------------------------------------------------

    async def handle_list_sessions(self, request: Any) -> Any:
        """GET /custom/v1/sessions — list historical sessions.

        Query parameters:
        - user_id (required): filter sessions by user ID
        - limit (optional, default 20, max 100): number of sessions to return
        - offset (optional, default 0): pagination offset
        """
        from aiohttp import web

        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        user_id = request.query.get("user_id", "").strip()
        if not user_id:
            return web.json_response(
                _openai_error(
                    "Query parameter 'user_id' is required",
                    code="missing_user_id",
                ),
                status=400,
            )

        try:
            raw_limit = request.query.get("limit", "20")
            limit = max(1, min(int(raw_limit), 100))
        except (ValueError, TypeError):
            return web.json_response(
                _openai_error("'limit' must be an integer", code="invalid_limit"),
                status=400,
            )

        try:
            raw_offset = request.query.get("offset", "0")
            offset = max(0, int(raw_offset))
        except (ValueError, TypeError):
            return web.json_response(
                _openai_error("'offset' must be an integer", code="invalid_offset"),
                status=400,
            )

        try:
            db = self._session_db_provider()
            if db is None:
                return web.json_response(
                    _openai_error(
                        "Session database unavailable", err_type="server_error",
                    ),
                    status=500,
                )
            sessions = db.list_sessions_rich(
                user_id=user_id,
                limit=limit,
                offset=offset,
            )
        except Exception as e:
            logger.error("Error listing sessions for user %s: %s", user_id, e)
            return web.json_response(
                _openai_error(
                    f"Internal server error: {e}", err_type="server_error",
                ),
                status=500,
            )

        return web.json_response({
            "object": "list",
            "data": sessions,
        })

    # ------------------------------------------------------------------
    # GET /custom/v1/sessions/{session_id}/messages
    # ------------------------------------------------------------------

    async def handle_get_session_messages(self, request: Any) -> Any:
        """GET /custom/v1/sessions/{session_id}/messages — retrieve messages.

        Walks the full parent_session_id chain (include_ancestors=True) so
        the response includes messages from all ancestor sessions in
        compression chains.
        """
        from aiohttp import web

        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        session_id = request.match_info["session_id"]

        try:
            db = self._session_db_provider()
            if db is None:
                return web.json_response(
                    _openai_error(
                        "Session database unavailable", err_type="server_error",
                    ),
                    status=500,
                )

            session = db.get_session(session_id)
            if session is None:
                return web.json_response(
                    _openai_error(
                        f"Session not found: {session_id}",
                        code="session_not_found",
                    ),
                    status=404,
                )

            messages = db.get_messages_as_conversation(
                session_id, include_ancestors=True,
            )
        except Exception as e:
            logger.error(
                "Error retrieving messages for session %s: %s", session_id, e,
            )
            return web.json_response(
                _openai_error(
                    f"Internal server error: {e}", err_type="server_error",
                ),
                status=500,
            )

        return web.json_response({
            "object": "session.messages",
            "session_id": session_id,
            "data": messages,
        })
