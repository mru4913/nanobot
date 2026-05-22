"""Uvicorn server for the Gleezy-compatible API on the gateway port."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uvicorn

from nanobot.gleezy_compat.app import create_app
from nanobot.gleezy_compat.bridge import GleezyWebBridge

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import Config
    from nanobot.cron.service import CronService
    from nanobot.session.manager import SessionManager


async def start_compat_server(
    host: str,
    port: int,
    *,
    bus: MessageBus,
    session_manager: SessionManager | None,
    config: Config | None,
    cron_service: CronService | None,
) -> None:
    """Run FastAPI (health + Gleezy REST) until cancelled."""
    bridge = GleezyWebBridge(bus)
    app = create_app(
        bridge=bridge,
        session_manager=session_manager,
        config=config,
        cron_service=cron_service,
    )
    server_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(server_config)
    await server.serve()
