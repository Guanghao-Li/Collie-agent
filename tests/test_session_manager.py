from __future__ import annotations

import pytest

from session.manager import SessionManager


@pytest.mark.asyncio
async def test_session_manager_saves_reads_and_clears(tmp_path) -> None:
    manager = SessionManager(tmp_path, max_recent_messages=2)
    await manager.initialize()

    manager.append_message("abc", "user", "one")
    manager.append_message("abc", "assistant", "two")
    manager.append_message("abc", "user", "three")

    recent = manager.get_messages("abc", limit=2)
    assert [message.content for message in recent] == ["two", "three"]
    assert "abc" in manager.list_sessions()

    manager.clear_session("abc")
    assert manager.get_messages("abc") == []

