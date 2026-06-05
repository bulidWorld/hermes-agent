"""
Abstract base class for custom API extensions.

Every extension that plugs into :class:`ExtensionAggregator` must
implement the four standard methods defined here.  Extension-specific
methods (like ``inject_attachments``) are discovered via :func:`hasattr`
and are **not** part of this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseExtension(ABC):
    """Standard extension interface consumed by
    :class:`~gateway.platforms.custom.extension_register.ExtensionAggregator`.
    """

    @abstractmethod
    def register_routes(self, app: Any) -> None:
        """Register routes on the aiohttp *app*."""
        ...

    @abstractmethod
    def extend_capabilities(self, caps: Dict[str, Any]) -> None:
        """Add feature flags to *caps*."""
        ...

    @abstractmethod
    def extend_endpoints(self, endpoints: Dict[str, Any]) -> None:
        """Add endpoint descriptors to *endpoints*."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources held by this extension."""
        ...
