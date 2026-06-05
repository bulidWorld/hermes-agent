"""
Custom API extensions for the API server platform.

Each extension is an independent, self-contained module that exposes a
standard interface: ``register_routes``, ``extend_capabilities``,
``extend_endpoints``, and ``close``.  Extensions are composed together
by :class:`ExtensionAggregator` so that :class:`~gateway.platforms.api_server.APIServerAdapter`
never needs to know about individual extension types.

Adding a new extension:

1. Create the extension class with the four standard methods.
2. Wire it into :meth:`ExtensionAggregator.from_config`.
3. If it has extension-specific callables (like ``inject_attachments``),
   add a delegating method on the aggregator.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from .session import CustomSessionHandlers, SessionExtension

logger = logging.getLogger(__name__)

_AuthChecker = Callable[..., Any]


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


class ExtensionAggregator:
    """Composes multiple extensions behind a single interface.

    Every standard call (``register_routes``, ``extend_capabilities``,
    ``extend_endpoints``, ``close``) is forwarded to **all** registered
    extensions.  Extension-specific methods like ``inject_attachments``
    delegate to the single extension that handles them.

    Adding a new extension type:

    1. Create the extension class (see module docstring).
    2. Add it to :meth:`from_config`.
    3. If it needs extension-specific hooks, add a delegating method here.
    """

    def __init__(self, extensions: List[Any]) -> None:
        self._extensions = extensions

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config_extra: Dict[str, Any],
        auth_checker: _AuthChecker,
        session_db_provider: Callable[[], Any],
    ) -> ExtensionAggregator:
        """Assemble all available extensions from gateway configuration.

        Always returns an aggregator (never ``None``) — at minimum the
        :class:`SessionExtension` is always available.
        """
        extensions: List[Any] = []

        # File-storage extension — only when file-storage service is configured
        from .file_storage import create_file_storage_components, FileStorageExtension  # noqa: E402

        storage_components = create_file_storage_components(
            config_extra, auth_checker,
        )
        if storage_components is not None:
            extensions.append(FileStorageExtension(storage_components))
        else:
            logger.debug(
                "File-storage extension: disabled "
                "(no file_storage_service_url configured)"
            )

        # Session extension — always available
        extensions.append(SessionExtension(
            CustomSessionHandlers(
                auth_checker=auth_checker,
                session_db_provider=session_db_provider,
            )
        ))

        logger.debug(
            "Extension aggregator: %d extension(s) loaded",
            len(extensions),
        )
        return cls(extensions)

    # ------------------------------------------------------------------
    # Standard delegating methods — called for every extension
    # ------------------------------------------------------------------

    def register_routes(self, app: Any) -> None:
        """Register routes from every extension on *app*."""
        for ext in self._extensions:
            ext.register_routes(app)

    def extend_capabilities(self, caps: Dict[str, Any]) -> None:
        """Merge capability flags from every extension into *caps*."""
        for ext in self._extensions:
            ext.extend_capabilities(caps)

    def extend_endpoints(self, endpoints: Dict[str, Any]) -> None:
        """Merge endpoint descriptors from every extension into *endpoints*."""
        for ext in self._extensions:
            ext.extend_endpoints(endpoints)

    async def close(self) -> None:
        """Release resources held by every extension."""
        for ext in self._extensions:
            await ext.close()

    # ------------------------------------------------------------------
    # Extension-specific methods — delegate to the owning extension
    # ------------------------------------------------------------------

    async def inject_attachments(
        self,
        user_message: str,
        attachments: List[Dict[str, Any]],
        request=None,
    ) -> str:
        """Delegate to the first file-capable extension.

        Returns *user_message* unchanged when file features are disabled
        or the attachments list is empty.
        """
        for ext in self._extensions:
            if hasattr(ext, "inject_attachments"):
                return await ext.inject_attachments(
                    user_message, attachments, request=request,
                )
        return user_message

    # ------------------------------------------------------------------
    # Convenience — allow callers to query which extensions are present
    # ------------------------------------------------------------------

    def has(self, extension_type: type) -> bool:
        """Return ``True`` when an extension of *extension_type* is loaded."""
        return any(isinstance(ext, extension_type) for ext in self._extensions)
