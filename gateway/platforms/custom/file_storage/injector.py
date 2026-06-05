"""
Inject uploaded file content into a user message string.

Design constraint — **always return ``str``**, never a multimodal
list.  :mod:`conversation_loop` silently skips memory / plugin context
injection when the user message is a list (``isinstance(_base, str)``
guard at ``conversation_loop.py:695``).  Keeping everything as a plain
string avoids that regression entirely.

Reuses :class:`~gateway.platforms.custom.file_parsing.FileClassifier`
(pure logic, no state) for classification and document parsing.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List

from gateway.platforms.custom.file_storage.file_parsing import FileClassifier

from .store import FileStorageStore

logger = logging.getLogger(__name__)

_INLINE_CHAR_LIMIT = 100_000

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_INLINE_TEMPLATE = (
    "[Attached file: {filename} ({size}, {mime})]\n"
    "Path: {path}\n"
    "Content:\n{content}\n"
    "[End: {filename}]"
)

_PATH_REF_TEMPLATE = (
    "[Attached file: {filename} ({size}, {mime})]\n"
    "Path: {path}\n"
    "Use the read_file tool to read this file."
)

_IMAGE_REF_TEMPLATE = (
    "[Attached image: {filename} ({size}, {mime})]\n"
    "Path: {path}\n"
    "Data URL: {data_url}\n"
    "Use vision_analyze or read the file above to view this image."
)


class FileStorageInjector:
    """Injects file content into a user-message string.

    Depends on a :class:`FileStorageStore` for metadata and cached file
    access.  Reuses :class:`FileClassifier` from the core ``custom``
    package for classification / document parsing (pure logic, no side
    effects).
    """

    def __init__(self, file_store: FileStorageStore) -> None:
        self._store = file_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def inject_attachments(
        self,
        user_message: str,
        attachments: List[Dict[str, Any]],
        request=None,
    ) -> str:
        """Process a list of ``{"file_id": "..."}`` dicts, injecting each
        file into *user_message* in order.

        Missing files are silently skipped (logged as warnings).
        """
        result = user_message
        for att in attachments:
            file_id = att.get("file_id") if isinstance(att, dict) else None
            if not isinstance(file_id, str) or not file_id.strip():
                continue
            file_id = file_id.strip()
            file_meta = self._store.get(file_id)
            if file_meta is None:
                logger.warning(
                    "inject_attachments: file not found — %s", file_id,
                )
                continue
            result = await self.inject_file(
                result, file_meta, request=request,
            )
        return result

    async def inject_file(
        self,
        user_message: str,
        file_meta: Dict[str, Any],
        request=None,
    ) -> str:
        """Inject a single file into *user_message*."""
        file_id = file_meta["file_id"]
        filename = file_meta["filename"]
        mime_type = file_meta["mime_type"]
        size_bytes = file_meta["size_bytes"]
        classification = FileClassifier.classify(mime_type, filename)
        size_label = FileClassifier.format_size(size_bytes)

        local_path = await self._store.ensure_local(file_id, request=request)
        if local_path is None:
            logger.warning(
                "inject_file: download failed — file_id=%s filename=%r → path_ref",
                file_id, filename,
            )
            return self._path_ref(
                user_message,
                filename,
                size_label,
                mime_type,
                str(self._store.get_local_path(file_id)),
            )
        path_str = str(local_path)

        # ---- document --------------------------------------------------
        if classification == "document":
            parsed = FileClassifier.parse_document(path_str, mime_type)
            if parsed is None:
                logger.warning(
                    "inject_file: document parse failed — file_id=%s filename=%r mime=%s → path_ref",
                    file_id, filename, mime_type,
                )
                return self._path_ref(
                    user_message, filename, size_label, mime_type, path_str,
                )
            if len(parsed) <= _INLINE_CHAR_LIMIT:
                logger.warning(
                    "inject_file: document inline — file_id=%s filename=%r len=%d/%d → inline",
                    file_id, filename, len(parsed), _INLINE_CHAR_LIMIT,
                )
                return self._inline(
                    user_message,
                    filename, size_label, mime_type, path_str, parsed,
                )
            logger.warning(
                "inject_file: document too large — file_id=%s filename=%r len=%d/%d → path_ref",
                file_id, filename, len(parsed), _INLINE_CHAR_LIMIT,
            )
            return self._path_ref(
                user_message, filename, size_label, mime_type, path_str,
            )

        # ---- plain text / code -----------------------------------------
        if classification == "text":
            content = await self._store.read_content(
                file_id, request=request,
            )
            if content is not None:
                text = content.decode("utf-8", errors="replace")
                if len(text) <= _INLINE_CHAR_LIMIT:
                    logger.warning(
                        "inject_file: text inline — file_id=%s filename=%r len=%d/%d → inline",
                        file_id, filename, len(text), _INLINE_CHAR_LIMIT,
                    )
                    return self._inline(
                        user_message,
                        filename, size_label, mime_type, path_str, text,
                    )
            logger.warning(
                "inject_file: text path_ref — file_id=%s filename=%r content=%s → path_ref",
                file_id, filename,
                "None" if content is None else f"len={len(text)}>{_INLINE_CHAR_LIMIT}",
            )
            return self._path_ref(
                user_message, filename, size_label, mime_type, path_str,
            )

        # ---- image -----------------------------------------------------
        if classification == "image":
            logger.warning(
                "inject_file: image — file_id=%s filename=%r mime=%s → image_ref",
                file_id, filename, mime_type,
            )
            data_url_preview = self._build_data_url_preview(
                file_id, path_str, mime_type,
            )
            block = _IMAGE_REF_TEMPLATE.format(
                filename=filename,
                size=size_label,
                mime=mime_type,
                path=path_str,
                data_url=data_url_preview,
            )
            return f"{user_message}\n\n{block}"

        # ---- binary ----------------------------------------------------
        logger.warning(
            "inject_file: binary — file_id=%s filename=%r mime=%s classification=%s → path_ref",
            file_id, filename, mime_type, classification,
        )
        return self._path_ref(
            user_message, filename, size_label, mime_type, path_str,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _path_ref(
        user_message: str,
        filename: str,
        size_label: str,
        mime_type: str,
        path: str,
    ) -> str:
        return (
            f"{user_message}\n\n"
            + _PATH_REF_TEMPLATE.format(
                filename=filename, size=size_label, mime=mime_type, path=path,
            )
        )

    @staticmethod
    def _inline(
        user_message: str,
        filename: str,
        size_label: str,
        mime_type: str,
        path: str,
        content: str,
    ) -> str:
        return (
            f"{user_message}\n\n"
            + _INLINE_TEMPLATE.format(
                filename=filename,
                size=size_label,
                mime=mime_type,
                path=path,
                content=content,
            )
        )

    @staticmethod
    def _build_data_url_preview(
        file_id: str, local_path: str, mime_type: str,
    ) -> str:
        """Length-only preview — not the full base64 blob."""
        try:
            data = Path(local_path).read_bytes()
            b64_len = len(base64.b64encode(data))
            ext = Path(local_path).suffix.lstrip(".").lower()
            img_mime = (
                f"image/{ext}"
                if ext in {"png", "jpeg", "gif", "webp"}
                else mime_type
            )
            return f"data:{img_mime};base64,[{b64_len} base64 chars]"
        except Exception as exc:
            logger.debug(
                "Cannot build data URL preview for %s: %s", file_id, exc,
            )
            return "(unavailable)"
