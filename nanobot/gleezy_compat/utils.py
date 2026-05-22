"""Pure utility functions for the Gleezy-compatible web API."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from nanobot.cron.types import CronJob


def serialize_job(job: CronJob) -> dict[str, Any]:
    """Serialise a CronJob to a JSON-friendly dict for API responses."""
    if job.schedule.kind == "every":
        secs = (job.schedule.every_ms or 0) // 1000
        if secs >= 3600:
            schedule_display = f"every {secs // 3600}h"
        elif secs >= 60:
            schedule_display = f"every {secs // 60}m"
        else:
            schedule_display = f"every {secs}s"
    elif job.schedule.kind == "cron":
        schedule_display = job.schedule.expr or ""
    else:
        schedule_display = "one-time"

    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule_kind": job.schedule.kind,
        "schedule_display": schedule_display,
        "schedule_expr": job.schedule.expr,
        "schedule_every_ms": job.schedule.every_ms,
        "message": job.payload.message,
        "deliver": job.payload.deliver,
        "channel": job.payload.channel,
        "to": job.payload.to,
        "next_run_at_ms": job.state.next_run_at_ms or None,
        "last_run_at_ms": job.state.last_run_at_ms or None,
        "last_status": job.state.last_status,
        "last_error": job.state.last_error,
        "created_at_ms": job.created_at_ms,
    }


def build_tree(
    directory: Path, workspace_dir: Path, exclude_dirs: set[str]
) -> list[dict[str, Any]]:
    """Recursively build a directory tree, directories first then files."""
    nodes: list[dict[str, Any]] = []
    try:
        entries = sorted(
            directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        )
    except PermissionError:
        return []

    for entry in entries:
        rel = entry.relative_to(workspace_dir)
        if rel.name.startswith("."):
            continue
        if entry.is_dir():
            if rel.parts[0] in exclude_dirs:
                continue
            nodes.append(
                {
                    "type": "dir",
                    "name": entry.name,
                    "path": str(rel),
                    "children": build_tree(entry, workspace_dir, exclude_dirs),
                }
            )
        elif entry.is_file():
            stat = entry.stat()
            mime, _ = mimetypes.guess_type(entry.name)
            nodes.append(
                {
                    "type": "file",
                    "name": entry.name,
                    "path": str(rel),
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                    "content_type": mime or "application/octet-stream",
                }
            )
    return nodes


MAX_EDITABLE_BYTES: int = 512 * 1024

EDITABLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "txt", "md", "json", "py", "js", "ts", "jsx", "tsx",
        "html", "htm", "css", "scss", "yml", "yaml", "env",
        "sh", "bash", "xml", "csv", "log", "sql", "graphql",
        "toml", "ini", "cfg", "conf",
    }
)


def is_editable_extension(path: Path) -> bool:
    return path.suffix.lstrip(".").lower() in EDITABLE_EXTENSIONS


_RUNTIME_CTX_MARKERS = (
    "[Runtime Context",
    "Current Time:",
)


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type")
            if t == "text":
                parts.append(part.get("text", ""))
            elif t == "image_url":
                parts.append("[image]")
            elif t == "input_audio":
                parts.append("[audio]")
        return "\n".join(parts)
    return str(content) if content else ""


def is_runtime_context(content: str) -> bool:
    stripped = content.strip()
    for marker in _RUNTIME_CTX_MARKERS:
        if stripped.startswith(marker):
            return True
    if "Channel:" in stripped and "Chat ID:" in stripped and len(stripped) < 300:
        return True
    return False


def filter_messages_for_display(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for m in messages:
        role = m.get("role")
        raw_content = m.get("content")
        text_for_check = extract_text_from_content(raw_content)

        if not text_for_check.strip():
            continue
        if role not in ("user", "assistant"):
            continue
        if role == "assistant" and m.get("tool_calls"):
            continue
        if role == "user" and is_runtime_context(text_for_check):
            continue
        if role == "assistant":
            stripped = text_for_check.strip()
            if (
                stripped.startswith("STDERR:")
                or stripped.startswith("STDOUT:")
                or stripped.startswith("Exit code:")
                or not stripped
            ):
                continue

        result.append(
            {
                "role": role,
                "content": raw_content,
                "timestamp": m.get("timestamp"),
            }
        )

    return result
