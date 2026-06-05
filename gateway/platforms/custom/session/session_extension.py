"""
Session extension — wraps :class:`CustomSessionHandlers` behind the
standard extension interface (``register_routes``, ``extend_capabilities``,
``extend_endpoints``, ``close``).

Always available — does not depend on any optional configuration.
"""

from __future__ import annotations

from typing import Any, Dict

from .handlers import CustomSessionHandlers
from ..base_extension import BaseExtension


class SessionExtension(BaseExtension):
    """Custom session listing and message retrieval.

    Always available — does not depend on any optional configuration.
    """

    def __init__(self, handlers: CustomSessionHandlers) -> None:
        self._sessions = handlers

    # -- Standard extension interface -----------------------------------

    def register_routes(self, app: Any) -> None:
        """Register ``/custom/v1/sessions*`` routes on the aiohttp *app*."""
        self._sessions.register_routes(app)

    def extend_capabilities(self, caps: Dict[str, Any]) -> None:
        """Add session-related feature flags to *caps*."""
        caps["custom_session_api"] = True

    def extend_endpoints(self, endpoints: Dict[str, Any]) -> None:
        """Add session endpoint descriptors to *endpoints*."""
        endpoints["custom_sessions"] = {
            "method": "GET", "path": "/custom/v1/sessions?user_id={user_id}",
        }
        endpoints["custom_session_messages"] = {
            "method": "GET",
            "path": "/custom/v1/sessions/{session_id}/messages",
        }

    async def close(self) -> None:
        """No-op — session handlers own no resources."""
        pass
