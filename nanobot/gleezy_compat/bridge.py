"""Inbound message bridge replacing the fork WebChannel for HTTP chat."""

from __future__ import annotations

from typing import Any

from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


class GleezyWebBridge(BaseChannel):
    """Publish browser chat messages to the message bus (no local WebSocket fan-out)."""

    name = "web"
    display_name = "Gleezy Web"

    def __init__(self, bus: MessageBus) -> None:
        super().__init__(config={"allow_from": ["*"]}, bus=bus)

    def is_allowed(self, sender_id: str) -> bool:  # noqa: ARG002
        return True

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: Any) -> None:
        """Outbound delivery is handled via Gateway WS translation (phase 3)."""
        return

    async def handle_chat_message(self, session_key: str, content: str) -> None:
        """Accept a chat message and forward it to the agent via the message bus."""
        chat_id = session_key.split(":", 1)[-1] if ":" in session_key else session_key
        await self._handle_message(
            sender_id="web_user",
            chat_id=chat_id,
            content=content,
            session_key=session_key,
        )

    async def notify_thinking(self, session_key: str) -> None:  # noqa: ARG002
        """No-op until phase 3 WebSocket bridge is wired."""
        return
