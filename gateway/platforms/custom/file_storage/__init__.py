"""
File-storage integration for the API server platform.

This sub-package provides file upload / download / list / delete endpoints
(``/v1/files/*``) backed by an external file-storage service, plus file
attachment injection into chat / runs / responses.

Design
------
* **Independent**: uses its own SQLite database (``file_storage_store.db``)
  and cache directory (``file_storage_cache/``), separate from the
  core ``custom`` package.
* **Token passthrough**: client JWT from ``AuthCenterToken`` header is
  forwarded directly to the file-storage service.  When no header is
  present, a fallback :class:`LoginTokenProvider` uses configured
  ``AUTH_CENTER_USER`` / ``AUTH_CENTER_PWD`` credentials.

Configuration
-------------
.. code-block:: yaml

    api_server:
      extra:
        file_storage_service_url: "https://files.example.com"
        auth_center_user: "hermes"          # optional fallback
        auth_center_pwd: "xxx"              # optional fallback
        file_storage_workspace: "hermes-agent"
        file_storage_folder: "/uploads"
"""

from .components import FileStorageComponents, create_file_storage_components
from .file_storage_extension import FileStorageExtension

__all__ = [
    "FileStorageComponents",
    "FileStorageExtension",
    "create_file_storage_components",
]
