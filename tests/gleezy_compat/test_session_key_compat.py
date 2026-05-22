import io
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from nanobot.gleezy_compat.routes import (
    _candidate_session_keys,
    _delete_session_candidates,
    _display_session_key,
    _install_skill_zip,
    _normalise_session_list,
)


def test_websocket_session_keys_display_as_legacy_web_keys():
    assert _display_session_key("websocket:default") == "web:default"
    assert _display_session_key("web:default") == "web:default"


def test_legacy_web_key_resolves_to_legacy_then_upstream_candidate():
    assert _candidate_session_keys("web:abc") == ["web:abc", "websocket:abc"]
    assert _candidate_session_keys("telegram:abc") == ["telegram:abc"]


def test_session_list_dedupes_web_and_websocket_keys():
    items = [
        {"key": "web:default", "updated_at": "2026-01-01T00:00:00"},
        {"key": "websocket:default", "updated_at": "2026-01-02T00:00:00"},
        {"key": "telegram:42", "updated_at": "2026-01-03T00:00:00"},
    ]

    normalised = _normalise_session_list(items)

    assert normalised == [
        {"key": "web:default", "updated_at": "2026-01-02T00:00:00"},
        {"key": "telegram:42", "updated_at": "2026-01-03T00:00:00"},
    ]


def test_delete_web_session_removes_both_session_variants_and_thread_files(tmp_path, monkeypatch):
    from nanobot.session.manager import SessionManager

    sm = SessionManager(tmp_path)
    legacy = sm.get_or_create("web:abc")
    legacy.add_message("user", "legacy")
    sm.save(legacy)
    upstream = sm.get_or_create("websocket:abc")
    upstream.add_message("user", "upstream")
    sm.save(upstream)

    deleted_threads: list[str] = []
    monkeypatch.setattr(
        "nanobot.gleezy_compat.routes.delete_webui_thread",
        lambda key: deleted_threads.append(key) or True,
    )

    assert _delete_session_candidates(sm, "web:abc") is True
    assert not sm._get_session_path("web:abc").exists()
    assert not sm._get_session_path("websocket:abc").exists()
    assert deleted_threads == ["web:abc", "websocket:abc"]


def test_install_skill_zip_preserves_existing_skill_when_upload_is_invalid(tmp_path):
    config = SimpleNamespace(workspace_path=tmp_path)
    existing = tmp_path / "skills" / "demo"
    existing.mkdir(parents=True)
    marker = existing / "skill.py"
    marker.write_text("old skill", encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        _install_skill_zip(config, "demo", b"not a zip")

    assert exc.value.status_code == 400
    assert marker.read_text(encoding="utf-8") == "old skill"


def test_install_skill_zip_preserves_existing_skill_when_zip_escapes_path(tmp_path):
    config = SimpleNamespace(workspace_path=tmp_path)
    existing = tmp_path / "skills" / "demo"
    existing.mkdir(parents=True)
    marker = existing / "skill.py"
    marker.write_text("old skill", encoding="utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.py", "bad")

    with pytest.raises(HTTPException) as exc:
        _install_skill_zip(config, "demo", buf.getvalue())

    assert exc.value.status_code == 403
    assert marker.read_text(encoding="utf-8") == "old skill"
