"""
Component assembly for the file-storage subsystem.

Reads configuration and wires together :class:`TokenProvider`,
:class:`FileStorageServiceClient`, :class:`FileStorageStore`,
:class:`FileStorageInjector`, and :class:`FileStorageHandlers` into
a single :class:`FileStorageComponents` bundle.

Returns ``None`` when no ``file_storage_service_url`` is configured
(file features are silently disabled).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .client import FileStorageServiceClient
from .token_provider import (
    FallbackTokenProvider,
    HeaderTokenProvider,
    LoginTokenProvider,
    TokenProvider,
)
from .store import FileStorageStore
from .injector import FileStorageInjector
from .handlers import FileStorageHandlers

logger = logging.getLogger(__name__)


@dataclass
class FileStorageComponents:
    """Bundle of file-storage components assembled by
    :func:`create_file_storage_components`."""

    store: FileStorageStore
    injector: FileStorageInjector
    handlers: FileStorageHandlers
    client: FileStorageServiceClient


def create_file_storage_components(
    config_extra: Dict[str, Any],
    auth_checker: Callable,
) -> Optional[FileStorageComponents]:
    """Assemble the file-storage subsystem from configuration.

    Reads configuration from *config_extra* or environment variables.
    Returns ``None`` when ``file_storage_service_url`` is not configured
    (file features are silently disabled).

    Configuration keys
    ------------------
    ============================== =============================== ==========
    config_extra / env var         Description                     Required
    ============================== =============================== ==========
    ``file_storage_service_url``   File-storage service base URL   **Yes**
    ``auth_center_user``           Default login username          No
    ``auth_center_pwd``            Default login password          No
    ``file_storage_workspace``     Workspace name (auto-created)   No
    ``file_storage_folder``        Folder path (auto-created)      No
    ============================== =============================== ==========
    """
    service_url = (
        config_extra.get("file_storage_service_url", "")
        or os.getenv("FILE_STORAGE_SERVICE_URL", "")
    ).strip()
    if not service_url:
        return None

    # ---- Token provider -----------------------------------------------
    token_provider: TokenProvider
    header_provider = HeaderTokenProvider()

    username = (
        config_extra.get("auth_center_user", "")
        or os.getenv("AUTH_CENTER_USER", "")
    ).strip()
    password = (
        config_extra.get("auth_center_pwd", "")
        or os.getenv("AUTH_CENTER_PWD", "")
    ).strip()

    if username and password:
        login_provider = LoginTokenProvider(service_url, username, password)
        token_provider = FallbackTokenProvider(header_provider, login_provider)
    else:
        token_provider = header_provider
        logger.debug(
            "File-storage: no AuthCenter credentials configured — "
            "only AuthCenterToken header passthrough is available"
        )

    # ---- Workspace / folder -------------------------------------------
    workspace = (
        config_extra.get("file_storage_workspace", "")
        or os.getenv("FILE_STORAGE_WORKSPACE", "hermes-agent")
    ).strip()
    folder = (
        config_extra.get("file_storage_folder", "")
        or os.getenv("FILE_STORAGE_FOLDER", "/uploads")
    ).strip()

    # ---- Assemble -----------------------------------------------------
    client = FileStorageServiceClient(
        base_url=service_url,
        token_provider=token_provider,
        workspace_name=workspace,
        folder_path=folder,
    )
    store = FileStorageStore(client=client)
    injector = FileStorageInjector(file_store=store)
    handlers = FileStorageHandlers(
        file_store=store,
        client=client,
        auth_checker=auth_checker,
    )

    logger.info(
        "File-storage extension enabled: service=%s workspace=%s folder=%s",
        service_url, workspace, folder,
    )
    return FileStorageComponents(
        store=store,
        injector=injector,
        handlers=handlers,
        client=client,
    )
