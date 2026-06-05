"""
Custom API extensions for the API server platform.

This package contains all user-added custom endpoints and subsystems
that extend the base :class:`APIServerAdapter`:

- File upload / download / list / delete (``/custom/v1/files/*``)
- File attachment injection into chat/runs/responses
- Custom session listing and message retrieval

Extensions are composed via :class:`ExtensionAggregator` so that the
adapter only ever talks to a single object.
"""

from .extension_register import ExtensionAggregator

__all__ = ["ExtensionAggregator"]
