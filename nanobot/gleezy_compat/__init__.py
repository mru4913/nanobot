"""Gleezy-compatible HTTP API for the multi-tenant platform frontend."""

from nanobot.gleezy_compat.app import create_app
from nanobot.gleezy_compat.server import start_compat_server

__all__ = ["create_app", "start_compat_server"]
