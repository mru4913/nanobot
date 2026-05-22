"""FastAPI application factory for the Gleezy-compatible HTTP API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nanobot.gleezy_compat.bridge import GleezyWebBridge
from nanobot.gleezy_compat.routes import register_routes

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import Config
    from nanobot.cron.service import CronService
    from nanobot.session.manager import SessionManager


def _get_version() -> str:
    from nanobot import __version__
    return __version__


def create_app(
    *,
    bridge: GleezyWebBridge,
    session_manager: SessionManager | None,
    config: Config | None,
    cron_service: CronService | None,
) -> FastAPI:
    """Create and configure the Gleezy-compatible FastAPI application."""
    app = FastAPI(
        title="nanobot-gleezy-compat",
        version=_get_version(),
        description="Gleezy platform HTTP API (status, workspace, cron, skills, sessions)",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.bridge = bridge
    app.state.session_manager = session_manager
    app.state.config = config
    app.state.cron_service = cron_service

    register_routes(app)
    return app
