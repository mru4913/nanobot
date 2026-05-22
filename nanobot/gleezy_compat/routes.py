"""HTTP routes for the Gleezy-compatible web API (ported from fork nanobot/web/routes.py)."""

from __future__ import annotations

import datetime
import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from nanobot.config.schema import Config
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule
from nanobot.gleezy_compat.bridge import GleezyWebBridge
from nanobot.gleezy_compat.models import AddCronJobRequest, ChatRequest, ToggleCronJobRequest
from nanobot.gleezy_compat.utils import (
    EDITABLE_EXTENSIONS,
    MAX_EDITABLE_BYTES,
    build_tree,
    filter_messages_for_display,
    is_editable_extension,
    serialize_job,
)
from nanobot.session.manager import SessionManager
from nanobot.webui.thread_disk import delete_webui_thread
from nanobot.webui.transcript import build_webui_thread_response


def get_bridge(request: Request) -> GleezyWebBridge:
    return request.app.state.bridge


def get_session_manager(request: Request) -> SessionManager:
    return request.app.state.session_manager


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_cron_service(request: Request) -> CronService:
    return request.app.state.cron_service


_Bridge = Annotated[GleezyWebBridge, Depends(get_bridge)]
_SessionManager = Annotated[SessionManager, Depends(get_session_manager)]
_Config = Annotated[Config, Depends(get_config)]
_CronService = Annotated[CronService, Depends(get_cron_service)]


def _resolve_skill_dir(config: Config, name: str) -> Path:
    skills_dir = (Path(config.workspace_path) / "skills").resolve()
    skill_path = (skills_dir / name).resolve()
    try:
        skill_path.relative_to(skills_dir)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc
    if not name or name.strip() in ("", "."):
        raise HTTPException(status_code=400, detail="Invalid skill name")
    if skill_path == skills_dir:
        raise HTTPException(status_code=400, detail="Cannot use skills root")
    return skill_path


def _install_skill_zip(config: Config, name: str, content: bytes) -> Path:
    """Validate and install a skill ZIP without deleting the previous skill first."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            members = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Invalid ZIP file") from exc

    skill_path = _resolve_skill_dir(config, name)
    skills_dir = (Path(config.workspace_path) / "skills").resolve()
    skills_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".{name}.upload.", dir=skills_dir) as tmp:
        staging_path = (Path(tmp) / name).resolve()
        staging_path.mkdir(parents=True, exist_ok=False)

        for member in members:
            dest = (staging_path / member.filename).resolve()
            try:
                dest.relative_to(staging_path)
            except ValueError as exc:
                raise HTTPException(
                    status_code=403, detail=f"Access denied: {member.filename}"
                ) from exc

        with zipfile.ZipFile(io.BytesIO(content)) as zf_extract:
            zf_extract.extractall(staging_path)

        backup_path: Path | None = None
        try:
            if skill_path.exists():
                backup_path = Path(
                    tempfile.mkdtemp(prefix=f".{name}.backup.", dir=skills_dir)
                ).resolve()
                backup_path.rmdir()
                shutil.move(str(skill_path), str(backup_path))
            shutil.move(str(staging_path), str(skill_path))
        except Exception:
            if backup_path is not None and backup_path.exists() and not skill_path.exists():
                shutil.move(str(backup_path), str(skill_path))
            raise
        finally:
            if backup_path is not None and backup_path.exists():
                shutil.rmtree(backup_path)

    return skill_path


def _display_session_key(key: str) -> str:
    if key.startswith("websocket:"):
        return f"web:{key.split(':', 1)[1]}"
    return key


def _candidate_session_keys(key: str) -> list[str]:
    if key.startswith("web:"):
        chat_id = key.split(":", 1)[1]
        return [key, f"websocket:{chat_id}"]
    return [key]


def _session_exists(sm: SessionManager, key: str) -> bool:
    return sm._get_session_path(key).exists() or key in sm._cache


def _resolve_session_key(sm: SessionManager, key: str) -> str:
    for candidate in _candidate_session_keys(key):
        if _session_exists(sm, candidate):
            return candidate
    return key


def _delete_session_candidates(sm: SessionManager, key: str) -> bool:
    deleted = False
    for candidate in _candidate_session_keys(key):
        path = sm._get_session_path(candidate)
        if path.exists():
            path.unlink()
            deleted = True
        if candidate in sm._cache:
            sm.invalidate(candidate)
            deleted = True
        if delete_webui_thread(candidate):
            deleted = True
    return deleted


def _normalise_session_list(items: list[Any]) -> list[Any]:
    by_key: dict[str, dict[str, Any]] = {}
    passthrough: list[Any] = []

    for item in items:
        if not isinstance(item, dict):
            passthrough.append(item)
            continue
        raw_key = item.get("key")
        if not isinstance(raw_key, str):
            passthrough.append(item)
            continue

        display_key = _display_session_key(raw_key)
        normalised = dict(item)
        normalised["key"] = display_key

        previous = by_key.get(display_key)
        if previous is None or str(normalised.get("updated_at", "")) >= str(previous.get("updated_at", "")):
            by_key[display_key] = normalised

    return [*by_key.values(), *passthrough]


def register_routes(app: FastAPI) -> None:
    """Attach all Gleezy-compatible API routes to *app*."""

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ping", tags=["health"])
    async def ping() -> dict[str, str]:
        return {"message": "pong"}

    @app.post("/api/chat", tags=["chat"])
    async def chat(req: ChatRequest, bridge: _Bridge) -> dict[str, str]:
        session_key = req.session_id
        await bridge.handle_chat_message(session_key, req.message)
        await bridge.notify_thinking(session_key)
        return {"status": "accepted", "session_id": session_key}

    @app.post("/api/chat/stream", tags=["chat"])
    async def chat_stream() -> None:
        raise HTTPException(
            status_code=501,
            detail="SSE chat stream is not available; use WebSocket (phase 3)",
        )

    @app.get("/api/sessions", tags=["sessions"])
    async def list_sessions(sm: _SessionManager) -> list[Any]:
        return _normalise_session_list(sm.list_sessions())

    @app.get("/api/sessions/{key}/webui-thread", tags=["sessions"])
    async def get_webui_thread(key: str) -> dict[str, Any]:
        for candidate in _candidate_session_keys(key):
            payload = build_webui_thread_response(candidate)
            if payload is None:
                continue
            session_key = payload.get("sessionKey")
            if isinstance(session_key, str):
                payload["sessionKey"] = _display_session_key(session_key)
            return payload
        raise HTTPException(status_code=404, detail="webui thread not found")

    @app.get("/api/sessions/{key:path}", tags=["sessions"])
    async def get_session(key: str, sm: _SessionManager) -> dict[str, Any]:
        resolved_key = _resolve_session_key(sm, key)
        session = sm.get_or_create(resolved_key)
        return {
            "key": _display_session_key(session.key),
            "messages": filter_messages_for_display(session.messages),
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
        }

    @app.delete("/api/sessions/{key:path}", tags=["sessions"])
    async def delete_session(key: str, sm: _SessionManager) -> dict[str, bool]:
        if _delete_session_candidates(sm, key):
            return {"ok": True}
        raise HTTPException(status_code=404, detail="Session not found")

    @app.get("/api/status", tags=["status"])
    async def get_status(config: _Config, cron: _CronService) -> dict[str, Any]:
        from nanobot.config.loader import get_config_path

        config_path = get_config_path()
        workspace_path = config.workspace_path.resolve()
        providers_dump = config.providers.model_dump()
        providers_list = [
            {
                "name": name,
                "has_key": bool(
                    p.get("api_key")
                    if isinstance(p, dict)
                    else getattr(p, "api_key", None)
                ),
            }
            for name, p in providers_dump.items()
        ]
        return {
            "config_path": str(config_path),
            "config_exists": config_path.exists(),
            "workspace": str(workspace_path),
            "workspace_exists": workspace_path.exists(),
            "providers": providers_list,
            "cron": cron.status(),
        }

    @app.get("/api/cron/jobs", tags=["cron"])
    async def list_cron_jobs(
        cron: _CronService,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            serialize_job(j) for j in cron.list_jobs(include_disabled=include_disabled)
        ]

    @app.post("/api/cron/jobs", tags=["cron"])
    async def add_cron_job(
        req: AddCronJobRequest,
        cron: _CronService,
    ) -> dict[str, Any]:
        if req.every_seconds is not None:
            schedule = CronSchedule(kind="every", every_ms=req.every_seconds * 1000)
        elif req.cron_expr is not None:
            schedule = CronSchedule(kind="cron", expr=req.cron_expr)
        elif req.at_iso is not None:
            dt = datetime.datetime.fromisoformat(req.at_iso)
            schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
        else:
            raise HTTPException(
                status_code=400,
                detail="Must specify one of: every_seconds, cron_expr, or at_iso",
            )
        job = cron.add_job(
            name=req.name,
            schedule=schedule,
            message=req.message,
            deliver=req.deliver,
            channel=req.channel,
            to=req.to,
        )
        return serialize_job(job)

    @app.delete("/api/cron/jobs/{job_id}", tags=["cron"])
    async def remove_cron_job(job_id: str, cron: _CronService) -> dict[str, bool]:
        if cron.remove_job(job_id):
            return {"ok": True}
        raise HTTPException(status_code=404, detail="Job not found")

    @app.put("/api/cron/jobs/{job_id}/toggle", tags=["cron"])
    async def toggle_cron_job(
        job_id: str,
        req: ToggleCronJobRequest,
        cron: _CronService,
    ) -> dict[str, Any]:
        job = cron.enable_job(job_id, enabled=req.enabled)
        if job:
            return serialize_job(job)
        raise HTTPException(status_code=404, detail="Job not found")

    @app.post("/api/cron/jobs/{job_id}/run", tags=["cron"])
    async def run_cron_job(job_id: str, cron: _CronService) -> dict[str, bool]:
        if await cron.run_job(job_id, force=True):
            return {"ok": True}
        raise HTTPException(status_code=404, detail="Job not found")

    @app.get("/api/skills", tags=["skills"])
    async def list_skills(config: _Config) -> list[dict[str, Any]]:
        from nanobot.agent.skills import SkillsLoader

        loader = SkillsLoader(config.workspace_path)
        raw = loader.list_skills(filter_unavailable=False)
        result: list[dict[str, Any]] = []
        for s in raw:
            meta = loader.get_skill_metadata(s["name"]) or {}
            available = loader._check_requirements(loader._get_skill_meta(s["name"]))
            result.append(
                {
                    "name": s["name"],
                    "description": meta.get("description", s["name"]),
                    "source": s["source"],
                    "available": available,
                    "path": s["path"],
                }
            )
        return result

    @app.post("/api/skills/upload", status_code=201, tags=["skills"])
    async def upload_skill(
        config: _Config,
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        """Extract a skill ZIP into workspace/skills/{skill_name}/."""
        if not file.filename or not file.filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="ZIP file required")

        stem = Path(file.filename).stem
        if not stem or stem.strip() in ("", "."):
            raise HTTPException(status_code=400, detail="Invalid skill name")

        content = await file.read()
        skill_path = _install_skill_zip(config, stem, content)

        from nanobot.agent.skills import SkillsLoader

        loader = SkillsLoader(config.workspace_path)
        meta = loader.get_skill_metadata(stem) or {}
        available = loader._check_requirements(loader._get_skill_meta(stem))
        return {
            "name": stem,
            "description": meta.get("description", stem),
            "source": "workspace",
            "available": available,
            "path": str(skill_path.relative_to(config.workspace_path)),
        }

    @app.delete("/api/skills/{name}", tags=["skills"])
    async def delete_skill(name: str, config: _Config) -> dict[str, bool]:
        skill_path = _resolve_skill_dir(config, name)
        if not skill_path.exists() or not skill_path.is_dir():
            raise HTTPException(status_code=404, detail="Skill not found")
        shutil.rmtree(skill_path)
        return {"ok": True}

    @app.get("/api/skills/{name}/download", tags=["skills"])
    async def download_skill_zip(name: str, config: _Config) -> StreamingResponse:
        skill_path = _resolve_skill_dir(config, name)
        if not skill_path.exists() or not skill_path.is_dir():
            raise HTTPException(status_code=404, detail="Skill not found")

        skills_dir = (Path(config.workspace_path) / "skills").resolve()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in skill_path.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(skills_dir))
        buf.seek(0)

        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
        )

    @app.get("/api/workspace", tags=["workspace"])
    async def list_workspace_files(config: _Config) -> list[Any]:
        workspace_dir = Path(config.workspace_path).resolve()
        if not workspace_dir.exists():
            return []
        return build_tree(workspace_dir, workspace_dir, {"skills"})

    @app.get("/api/workspace/content/{file_path:path}", tags=["workspace"])
    async def read_workspace_file_content(
        file_path: str, config: _Config
    ) -> dict[str, Any]:
        workspace_dir = Path(config.workspace_path).resolve()

        ws_str = str(workspace_dir)
        if file_path.startswith(ws_str):
            file_path = file_path[len(ws_str) :].lstrip("/")

        target_path = (workspace_dir / file_path).resolve()

        try:
            target_path.relative_to(workspace_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")

        if not target_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        if not target_path.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file")
        if not is_editable_extension(target_path):
            raise HTTPException(
                status_code=415,
                detail=f"File type not editable. Allowed: {sorted(EDITABLE_EXTENSIONS)}",
            )

        size = target_path.stat().st_size
        if size > MAX_EDITABLE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large for in-browser editing ({size} bytes, max {MAX_EDITABLE_BYTES})",
            )

        try:
            content = target_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = target_path.read_text(encoding="utf-8", errors="replace")

        import mimetypes as _mimetypes

        mime, _ = _mimetypes.guess_type(target_path.name)

        return {
            "path": str(target_path.relative_to(workspace_dir)),
            "content": content,
            "content_type": mime or "text/plain",
        }

    @app.put(
        "/api/workspace/content/{file_path:path}", status_code=204, tags=["workspace"]
    )
    async def write_workspace_file_content(
        file_path: str, request: Request, config: _Config
    ) -> Response:
        workspace_dir = Path(config.workspace_path).resolve()

        ws_str = str(workspace_dir)
        if file_path.startswith(ws_str):
            file_path = file_path[len(ws_str) :].lstrip("/")

        target_path = (workspace_dir / file_path).resolve()

        try:
            target_path.relative_to(workspace_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")

        if not target_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        if not target_path.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file")
        if not is_editable_extension(target_path):
            raise HTTPException(status_code=415, detail="File type not editable")

        body = await request.body()
        if len(body) > MAX_EDITABLE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Content too large (max {MAX_EDITABLE_BYTES} bytes)",
            )

        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                payload = json.loads(body)
                text = payload.get("content", "")
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
        else:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=400, detail="Body must be UTF-8 text") from exc

        target_path.write_text(text, encoding="utf-8")
        return Response(status_code=204)

    @app.get("/api/workspace/{file_path:path}", tags=["workspace"])
    async def download_workspace_file(file_path: str, config: _Config) -> FileResponse:
        workspace_dir = Path(config.workspace_path).resolve()

        ws_str = str(workspace_dir)
        if file_path.startswith(ws_str):
            file_path = file_path[len(ws_str) :].lstrip("/")

        target_path = (workspace_dir / file_path).resolve()

        try:
            target_path.relative_to(workspace_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")

        if not target_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        return FileResponse(path=target_path, filename=target_path.name)

    @app.delete("/api/workspace/{file_path:path}", tags=["workspace"])
    async def delete_workspace_path(file_path: str, config: _Config) -> Response:
        workspace_dir = Path(config.workspace_path).resolve()

        if not file_path or file_path.strip("/") in ("", "."):
            raise HTTPException(status_code=400, detail="Cannot delete workspace root")

        ws_str = str(workspace_dir)
        if file_path.startswith(ws_str):
            file_path = file_path[len(ws_str) :].lstrip("/")

        target_path = (workspace_dir / file_path).resolve()

        try:
            target_path.relative_to(workspace_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")

        if target_path == workspace_dir:
            raise HTTPException(status_code=400, detail="Cannot delete workspace root")
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="Path not found")

        if target_path.is_file():
            target_path.unlink()
        else:
            shutil.rmtree(target_path)

        return Response(status_code=204)

    @app.post("/api/workspace/upload", status_code=201, tags=["workspace"])
    async def upload_workspace_file(
        config: _Config,
        file: UploadFile = File(...),
        relative_path: str = "",
    ) -> dict[str, str]:
        workspace_dir = Path(config.workspace_path).resolve()

        clean_rel = relative_path.strip().strip("/")
        target_dir = (
            (workspace_dir / clean_rel).resolve() if clean_rel else workspace_dir
        )

        try:
            target_dir.relative_to(workspace_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")

        if target_dir.exists() and target_dir.is_file():
            raise HTTPException(
                status_code=400, detail="Target path is a file, not a directory"
            )

        if target_dir != workspace_dir and not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)

        filename = Path(file.filename).name if file.filename else ""
        if not filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        dest = (target_dir / filename).resolve()

        try:
            dest.relative_to(workspace_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")

        content = await file.read()
        dest.write_bytes(content)

        return {"path": str(dest.relative_to(workspace_dir))}

    @app.post("/api/workspace/upload-zip", status_code=201, tags=["workspace"])
    async def upload_workspace_zip(
        config: _Config,
        file: UploadFile = File(...),
    ) -> dict[str, int]:
        max_file_count = 500
        max_uncompressed_bytes = 100 * 1024 * 1024

        workspace_dir = Path(config.workspace_path).resolve()

        content = await file.read()
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="Invalid ZIP file") from exc

        members = zf.infolist()

        if len(members) > max_file_count:
            raise HTTPException(
                status_code=413,
                detail=f"ZIP contains too many files (max {max_file_count})",
            )
        total_size = sum(m.file_size for m in members)
        if total_size > max_uncompressed_bytes:
            raise HTTPException(
                status_code=413, detail="ZIP uncompressed size exceeds 100 MB limit"
            )

        for member in members:
            dest = (workspace_dir / member.filename).resolve()
            try:
                dest.relative_to(workspace_dir)
            except ValueError as exc:
                raise HTTPException(
                    status_code=403, detail=f"Access denied: {member.filename}"
                ) from exc

        zf.extractall(workspace_dir)
        return {"extracted": len(members)}
