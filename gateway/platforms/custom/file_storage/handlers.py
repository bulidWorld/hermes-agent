"""
HTTP request handlers for ``/custom/v1/files/*`` endpoints.

Registered on the aiohttp ``Application`` via
:meth:`FileStorageHandlers.register_routes`.  Depends on
:class:`FileStorageStore` and :class:`FileStorageServiceClient` — does
not depend on :class:`APIServerAdapter`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

try:
    from aiohttp import web

    _AIOHTTP = True
except ImportError:
    web = None  # type: ignore[assignment]
    _AIOHTTP = False

from .store import FileStorageStore, _generate_file_id
from .client import FileStorageServiceClient

logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


def _openai_error(
    message: str,
    err_type: str = "invalid_request_error",
    code: Optional[str] = None,
) -> Dict:
    return {"error": {"message": message, "type": err_type, "code": code}}


class FileStorageHandlers:
    """Async handlers for the file upload / list / download / delete API.

    Parameters
    ----------
    file_store:
        Metadata store + cache manager.
    client:
        File-storage service client (used for upload / download proxying).
    auth_checker:
        Callable ``(request) -> Optional[Response]`` — returns ``None``
        when authentication succeeds.
    """

    def __init__(
        self,
        file_store: FileStorageStore,
        client: FileStorageServiceClient,
        auth_checker: Callable[..., Any],
    ) -> None:
        self._store = file_store
        self._client = client
        self._check_auth = auth_checker

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def register_routes(self, app: Any) -> None:
        """Register ``/custom/v1/files/*`` routes on the aiohttp *app*."""
        if web is None:
            return
        app.router.add_post("/custom/v1/files", self.upload)
        app.router.add_get("/custom/v1/files", self.list_files)
        app.router.add_get("/custom/v1/files/{file_id}", self.download)
        app.router.add_delete("/custom/v1/files/{file_id}", self.delete)

    # ------------------------------------------------------------------
    # POST /custom/v1/files
    # ------------------------------------------------------------------

    async def upload(self, request: "web.Request") -> "web.Response":
        """**POST** ``/custom/v1/files`` — multipart/form-data.

        Fields: ``file`` (one or more), ``ttl`` (optional, seconds).
        """
        auth_err = self._check_auth(request)
        if auth_err is not None:
            return auth_err

        try:
            reader = await request.multipart()
        except Exception:
            return web.json_response(
                _openai_error(
                    "Expected multipart/form-data request body.",
                    code="invalid_content_type",
                ),
                status=400,
            )

        ttl_seconds: Optional[int] = None
        uploaded: List[Dict] = []
        errors: List[str] = []
        total_bytes = 0

        async for part in reader:
            if part is None:
                break

            field_name = (part.name or "").strip()

            if field_name == "ttl":
                ttl_text = await part.text()
                try:
                    v = int(ttl_text.strip())
                    if v > 0:
                        ttl_seconds = v
                except (ValueError, TypeError):
                    pass
                continue

            if field_name != "file":
                continue

            filename = (part.filename or "unnamed").strip()
            if not filename:
                continue

            data = bytearray()
            chunk_count = 0
            while True:
                chunk = await part.read_chunk(size=64 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
                total_bytes += len(chunk)
                chunk_count += 1
                if total_bytes > _MAX_UPLOAD_BYTES:
                    return web.json_response(
                        _openai_error(
                            f"Upload exceeds maximum size of "
                            f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                            code="file_too_large",
                        ),
                        status=413,
                    )

            if chunk_count == 0:
                errors.append(f"{filename}: empty file")
                continue

            file_bytes = bytes(data)
            mime_type = (
                part.headers.get("Content-Type")
                or part.headers.get("content-type")
                or "application/octet-stream"
            )

            file_id = _generate_file_id()

            try:
                result = await self._client.upload(
                    file_id, filename, mime_type, file_bytes,
                    request=request,
                )
            except Exception as exc:
                logger.error(
                    "File-storage upload failed for %s: %s", filename, exc,
                )
                return web.json_response(
                    _openai_error(
                        f"Failed to store file on remote server: {exc}",
                        err_type="server_error",
                        code="remote_upload_failed",
                    ),
                    status=502,
                )

            meta = self._store.put(
                file_id=file_id,
                public_id=result.public_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=len(file_bytes),
                remote_url=result.remote_url,
                ttl_seconds=ttl_seconds,
            )
            uploaded.append(meta)

        if not uploaded and not errors:
            return web.json_response(
                _openai_error(
                    "No file found in the request. "
                    "Send one or more 'file' parts.",
                    code="no_file_provided",
                ),
                status=400,
            )

        result: Dict = {"object": "list", "data": uploaded}
        if errors:
            result["warnings"] = errors
        return web.json_response(result, status=201)

    # ------------------------------------------------------------------
    # GET /custom/v1/files
    # ------------------------------------------------------------------

    async def list_files(self, request: "web.Request") -> "web.Response":
        """**GET** ``/custom/v1/files`` — list all uploaded file metadata."""
        auth_err = self._check_auth(request)
        if auth_err is not None:
            return auth_err
        return web.json_response({
            "object": "list",
            "data": self._store.list_all(),
        })

    # ------------------------------------------------------------------
    # GET /custom/v1/files/{file_id}
    # ------------------------------------------------------------------

    async def download(self, request: "web.Request") -> "web.Response":
        """**GET** ``/custom/v1/files/{file_id}`` — download a file.

        Proxies from the file-storage service using the stored public_id.
        """
        auth_err = self._check_auth(request)
        if auth_err is not None:
            return auth_err

        file_id = request.match_info["file_id"]
        meta = self._store.get(file_id)
        if meta is None:
            return web.json_response(
                _openai_error(
                    f"File not found: {file_id}", code="file_not_found",
                ),
                status=404,
            )

        remote_id = meta["public_id"]
        data = await self._client.download(remote_id, request=request)
        if data is None:
            return web.json_response(
                _openai_error(
                    "File metadata exists but remote download failed.",
                    err_type="server_error",
                    code="remote_download_failed",
                ),
                status=502,
            )

        return web.Response(
            body=data,
            content_type=meta["mime_type"],
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{meta["filename"]}"'
                ),
                "X-File-Id": file_id,
            },
        )

    # ------------------------------------------------------------------
    # DELETE /v1/files/{file_id}
    # ------------------------------------------------------------------

    async def delete(self, request: "web.Request") -> "web.Response":
        """**DELETE** ``/v1/files/{file_id}`` — delete from remote,
        metadata, and local cache."""
        auth_err = self._check_auth(request)
        if auth_err is not None:
            return auth_err

        file_id = request.match_info["file_id"]
        meta = self._store.get(file_id)
        if meta is None:
            return web.json_response(
                _openai_error(
                    f"File not found: {file_id}", code="file_not_found",
                ),
                status=404,
            )

        remote_id = meta["public_id"]
        try:
            await self._client.delete(remote_id, request=request)
        except Exception as exc:
            logger.warning(
                "File-storage delete failed for %s: %s", file_id, exc,
            )

        self._store.delete(file_id)
        self._store._remove_cache_file(file_id)

        return web.json_response({
            "id": file_id,
            "object": "file",
            "deleted": True,
        })
