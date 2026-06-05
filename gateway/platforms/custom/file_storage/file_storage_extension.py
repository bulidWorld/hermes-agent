"""
File-storage extension for the API server.

Wires the file-storage components into the extension system so that
:class:`~gateway.platforms.api_server.APIServerAdapter` discovers and
uses them without any knowledge of the file-storage internals.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .components import FileStorageComponents
from ..base_extension import BaseExtension

logger = logging.getLogger(__name__)


class FileStorageExtension(BaseExtension):
    """File upload / download / list / delete via the file-storage service.

    Only available when ``file_storage_service_url`` is configured
    (either in *config_extra* or the ``FILE_STORAGE_SERVICE_URL``
    environment variable).
    """

    def __init__(self, components: FileStorageComponents) -> None:
        self._components = components

    # -- Standard extension interface -----------------------------------

    def register_routes(self, app: Any) -> None:
        """Register ``/v1/files/*`` routes on the aiohttp *app*."""
        self._components.handlers.register_routes(app)

    def extend_capabilities(self, caps: Dict[str, Any]) -> None:
        """Add file-related feature flags to *caps*."""
        caps["file_upload"] = True
        caps["file_attachments"] = True

    def extend_endpoints(self, endpoints: Dict[str, Any]) -> None:
        """Add file endpoint descriptors to *endpoints*."""
        endpoints["upload_file"] = {
            "method": "POST",
            "path": "/v1/files",
        }
        endpoints["list_files"] = {
            "method": "GET",
            "path": "/v1/files",
        }
        endpoints["get_file"] = {
            "method": "GET",
            "path": "/v1/files/{file_id}",
        }
        endpoints["delete_file"] = {
            "method": "DELETE",
            "path": "/v1/files/{file_id}",
        }

    async def close(self) -> None:
        """Release file-store and client resources."""
        self._components.store.close()
        await self._components.client.close()

    # -- File-specific --------------------------------------------------

    async def inject_attachments(
        self,
        user_message: str,
        attachments: List[Dict[str, Any]],
        request=None,
    ) -> str:
        """Resolve file references in *attachments* and inject content
        into *user_message*.

        Returns *user_message* unchanged when the attachments list is empty.
        """
        if not attachments:
            return user_message
        return await self._components.injector.inject_attachments(
            user_message, attachments, request=request,
        )
