"""
File-storage service HTTP client.

Implements the :class:`RemoteStorageClient` abstract interface using the
actual file-storage service API (multipart upload, JWT auth, publicId-based
addressing).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from .token_provider import TokenProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

_RETRY_BACKOFF = 0.5  # seconds base, multiplied by attempt number


@dataclass
class FileUploadResult:
    """Result returned by :meth:`FileStorageServiceClient.upload`.

    Attributes
    ----------
    remote_url:
        Download URL that can be returned to hermes API consumers.
    public_id:
        The file-storage service's ``publicId`` (UUID), used for subsequent
        download / delete calls.
    """

    remote_url: str
    public_id: str


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class FileStorageServiceClient:
    """Async HTTP client for the file-storage service.

    Parameters
    ----------
    base_url:
        Root URL of the file-storage service, e.g.
        ``https://files.example.com``.
    token_provider:
        :class:`TokenProvider` strategy used to obtain a JWT for each request.
    workspace_name:
        Workspace name passed on upload (auto-created by the service).
    folder_path:
        Folder path passed on upload (auto-created recursively).
    timeout:
        Total timeout per request in seconds.
    max_retries:
        Number of retries for idempotent GET / DELETE requests.
    """

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        workspace_name: str = "hermes-agent",
        folder_path: str = "/uploads",
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._workspace_name = workspace_name
        self._folder_path = folder_path
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max_retries
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
        await self._token_provider.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _auth_headers(self, request=None) -> dict:
        token = await self._token_provider.get_token(request)
        if not token:
            raise RuntimeError(
                "File-storage authentication failed: no token available. "
                "Ensure AuthCenterToken header is present or "
                "AUTH_CENTER_USER / AUTH_CENTER_PWD are configured."
            )
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def upload(
        self,
        file_id: str,
        filename: str,
        mime_type: str,
        data: bytes,
        request=None,
    ) -> FileUploadResult:
        """Upload a file via ``POST /api/v1/files`` (multipart/form-data)."""
        session = await self._ensure_session()
        url = f"{self._base_url}/api/v1/files"

        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type=mime_type)
        form.add_field("workspaceName", self._workspace_name)
        form.add_field("folderPath", self._folder_path)

        headers = await self._auth_headers(request)

        async with session.post(url, data=form, headers=headers) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                raise RuntimeError(
                    f"File-storage upload failed: HTTP {resp.status} — "
                    f"{body[:500]}"
                )
            result = await resp.json()

        public_id = result["data"]["publicId"]
        download_url = f"{self._base_url}/api/v1/files/{public_id}/download"
        logger.debug(
            "Uploaded %s (%d bytes) → publicId=%s", filename, len(data), public_id,
        )
        return FileUploadResult(remote_url=download_url, public_id=public_id)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download(self, remote_id: str, request=None) -> Optional[bytes]:
        """Download file by *remote_id* (the publicId)."""
        session = await self._ensure_session()
        url = f"{self._base_url}/api/v1/files/{remote_id}/download"

        for attempt in range(self._max_retries):
            headers = await self._auth_headers(request)
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 404:
                        return None
                    if resp.status != 200:
                        logger.warning(
                            "File-storage download %s returned %d",
                            remote_id, resp.status,
                        )
                        return None
                    return await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.debug(
                    "File-storage download %s attempt %d/%d: %s",
                    remote_id, attempt + 1, self._max_retries, exc,
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
        return None

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, remote_id: str, request=None) -> bool:
        """Soft-delete file by *remote_id* (the publicId)."""
        session = await self._ensure_session()
        url = f"{self._base_url}/api/v1/files/{remote_id}"

        for attempt in range(self._max_retries):
            headers = await self._auth_headers(request)
            try:
                async with session.delete(url, headers=headers) as resp:
                    if resp.status in (200, 204):
                        return True
                    if resp.status == 404:
                        return False  # already gone
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.debug(
                    "File-storage delete %s attempt %d/%d: %s",
                    remote_id, attempt + 1, self._max_retries, exc,
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
        return False

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Return ``True`` when the file-storage service is reachable."""
        try:
            session = await self._ensure_session()
            url = f"{self._base_url}/api/v1/files?limit=1"
            headers = await self._auth_headers()
            async with session.get(url, headers=headers) as resp:
                return resp.status < 500
        except Exception:
            return False
