"""
Token provider strategies for authenticating with the file-storage service.

Follows the **open-closed principle**: new token sources are added by
subclassing :class:`TokenProvider` — no existing code is modified.

Built-in strategies
-------------------
* :class:`HeaderTokenProvider` — read JWT from ``AuthCenterToken`` request header
* :class:`LoginTokenProvider` — obtain JWT via ``POST /api/v1/auth/login``
* :class:`FallbackTokenProvider` — try providers in order, first non-empty wins
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class TokenProvider(ABC):
    """Abstract strategy for obtaining a JWT token.

    Subclass and override :meth:`get_token` to add new token sources
    (e.g. OAuth2 client-credentials flow, service-account key, …) without
    touching :class:`~.client.FileStorageServiceClient`.
    """

    @abstractmethod
    async def get_token(self, request=None) -> str:
        """Return a JWT token string, or ``""`` when unavailable."""
        ...

    async def close(self) -> None:
        """Release any held resources (sessions, …)."""


# ---------------------------------------------------------------------------
# Header-based provider — pass through the client's own token
# ---------------------------------------------------------------------------


class HeaderTokenProvider(TokenProvider):
    """Extract the JWT from the ``AuthCenterToken`` request header.

    This is the **primary** strategy: when an external client calls
    hermes-agent with their own file-storage JWT, we forward it unchanged.
    """

    HEADER_NAME: str = "AuthCenterToken"

    async def get_token(self, request=None) -> str:
        if request is None:
            return ""
        token = request.headers.get(self.HEADER_NAME, "")
        return token.strip() if token else ""


# ---------------------------------------------------------------------------
# Login-based provider — obtain a token with service credentials
# ---------------------------------------------------------------------------


class LoginTokenProvider(TokenProvider):
    """Log in to the file-storage service and cache the JWT in memory.

    Used as a **fallback** when the client does not supply an
    ``AuthCenterToken`` header (e.g. background sweep tasks, or clients
    that rely on hermes-agent's own identity).

    Parameters
    ----------
    base_url:
        Root URL of the file-storage service.
    username:
        Login username (from ``AUTH_CENTER_USER`` config).
    password:
        Login password (from ``AUTH_CENTER_PWD`` config).
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_token(self, request=None) -> str:
        """Return a cached token, or log in to obtain a fresh one."""
        if self._token and time.time() < self._expires_at - 60:
            return self._token

        async with self._lock:
            # Double-check after acquiring the lock (concurrent callers)
            if self._token and time.time() < self._expires_at - 60:
                return self._token

            session = await self._ensure_session()
            login_url = f"{self._base_url}/api/v1/auth/login"
            payload = {"username": self._username, "password": self._password}

            try:
                async with session.post(login_url, json=payload) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(
                            f"File-storage login failed: HTTP {resp.status} — "
                            f"{text[:500]}"
                        )
                    data = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise RuntimeError(
                    f"File-storage login unreachable: {exc}"
                ) from exc

            self._token = data["access_token"]
            # Token TTL is 24 h; refresh 5 min early for safety.
            self._expires_at = time.time() + 86400 - 300
            logger.debug("File-storage login: token acquired, expires in ~24 h")
            return self._token

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30.0),
            )
        return self._session


# ---------------------------------------------------------------------------
# Composite — try primary first, then fallback
# ---------------------------------------------------------------------------


class FallbackTokenProvider(TokenProvider):
    """Composite provider that tries *primary* first, then *fallback*.

    >>> header = HeaderTokenProvider()
    >>> login = LoginTokenProvider(url, user, pwd)
    >>> provider = FallbackTokenProvider(header, login)
    """

    def __init__(
        self,
        primary: TokenProvider,
        fallback: TokenProvider,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def get_token(self, request=None) -> str:
        token = await self._primary.get_token(request)
        if token:
            return token
        return await self._fallback.get_token(request)

    async def close(self) -> None:
        await self._primary.close()
        await self._fallback.close()
