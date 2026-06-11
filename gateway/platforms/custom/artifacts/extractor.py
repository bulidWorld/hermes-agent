"""Extract generated file artifacts from API run results."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

_FILE_MUTATION_TOOLS = {"write_file", "patch", "terminal"}
_STRUCTURED_FILE_TOOLS = {"write_file", "patch"}
_ARTIFACT_EXTS = {
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
    ".zip",
}
_PATH_PATTERN = re.compile(r"(?:~|/)[^\s\"'<>|]+")


def extract_artifact_candidates(result: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return file artifacts produced by file mutation tool outputs.

    ``run_conversation`` already returns the full message list.  Tool result
    messages contain JSON strings, and file tools report paths via
    ``resolved_path``, ``files_modified``, and ``files_created``.
    """
    artifacts: List[Dict[str, str]] = []
    seen: set[str] = set()

    for msg in result.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        data = _json_object(msg.get("content"))
        if not data or data.get("error"):
            continue
        tool_call_id = str(msg.get("tool_call_id") or "")
        tool_name = _tool_name_for_message(msg, result) or ""
        if tool_name and tool_name not in _FILE_MUTATION_TOOLS:
            continue
        for candidate in _candidate_paths(data, tool_name):
            resolved = _existing_file(candidate)
            if resolved and _is_artifact_file(resolved) and resolved not in seen:
                seen.add(resolved)
                artifacts.append({
                    "path": resolved,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                })

    return artifacts


def extract_artifact_paths(result: Dict[str, Any]) -> List[str]:
    """Backward-compatible path-only view of extracted artifacts."""
    return [item["path"] for item in extract_artifact_candidates(result)]


def _json_object(value: Any) -> Dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _candidate_paths(data: Dict[str, Any], tool_name: str) -> Iterable[str]:
    if tool_name == "terminal":
        yield from _terminal_output_paths(data)
        return
    if tool_name and tool_name not in _STRUCTURED_FILE_TOOLS:
        return

    single = data.get("resolved_path")
    if isinstance(single, str):
        yield single

    for key in ("files_modified", "files_created"):
        values = data.get(key)
        if isinstance(values, list):
            for item in values:
                if isinstance(item, str):
                    yield item


def _terminal_output_paths(data: Dict[str, Any]) -> Iterable[str]:
    if data.get("error") or data.get("exit_code") not in (0, "0", None):
        return
    output = data.get("output")
    if not isinstance(output, str) or not output:
        return
    for match in _PATH_PATTERN.finditer(output):
        candidate = match.group(0).rstrip(".,;:，。；：)]}）】")
        if _is_artifact_file(candidate):
            yield candidate


def _is_artifact_file(path: str) -> bool:
    return Path(path).suffix.lower() in _ARTIFACT_EXTS


def _existing_file(path: str) -> str | None:
    try:
        resolved = str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if os.path.isfile(resolved) else None


def _tool_name_for_message(msg: Dict[str, Any], result: Dict[str, Any]) -> str | None:
    """Best-effort map from tool message to its assistant tool call name."""
    call_id = msg.get("tool_call_id")
    if not call_id:
        return None
    for prior in result.get("messages") or []:
        if not isinstance(prior, dict) or prior.get("role") != "assistant":
            continue
        for call in prior.get("tool_calls") or []:
            if not isinstance(call, dict) or call.get("id") != call_id:
                continue
            function = call.get("function") or {}
            if isinstance(function, dict):
                name = function.get("name")
                return name if isinstance(name, str) else None
    return None
