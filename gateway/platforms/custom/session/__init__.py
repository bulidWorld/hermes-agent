"""
Session API extension — ``/custom/v1/sessions/*`` endpoints.

Exports :class:`CustomSessionHandlers` (HTTP handlers) and
:class:`SessionExtension` (standard extension interface wrapper).
"""

from .session_extension import SessionExtension
from .handlers import CustomSessionHandlers

__all__ = ["CustomSessionHandlers", "SessionExtension"]
