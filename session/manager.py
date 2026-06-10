from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from session.models import SessionMessage, SessionRole


class SessionManager:
    def __init__(self, workspace: str | Path, max_recent_messages: int = 30) -> None:
        self.workspace = Path(workspace)
        self.session_dir = self.workspace / "sessions"
        self.max_recent_messages = max_recent_messages
        self._cache: dict[str, list[SessionMessage]] = {}

    async def initialize(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, session_id: str) -> Path:
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", session_id).strip("_") or "default"
        return self.session_dir / f"{safe_id}.json"

    def _load(self, session_id: str) -> list[SessionMessage]:
        if session_id in self._cache:
            return self._cache[session_id]
        path = self._path_for(session_id)
        if not path.exists():
            self._cache[session_id] = []
            return self._cache[session_id]
        data = json.loads(path.read_text(encoding="utf-8"))
        messages = [SessionMessage.from_dict(item) for item in data.get("messages", [])]
        self._cache[session_id] = messages
        return messages

    def append_message(
        self,
        session_id: str,
        role: SessionRole,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        messages = self._load(session_id)
        messages.append(SessionMessage(role=role, content=content, metadata=metadata or {}))
        self.save_session(session_id)

    def get_messages(self, session_id: str, limit: int | None = None) -> list[SessionMessage]:
        messages = self._load(session_id)
        count = limit if limit is not None else self.max_recent_messages
        return messages[-count:]

    def clear_session(self, session_id: str) -> None:
        self._cache[session_id] = []
        self.save_session(session_id)

    def list_sessions(self) -> list[str]:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        return sorted(path.stem for path in self.session_dir.glob("*.json"))

    def save_session(self, session_id: str) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        messages = self._load(session_id)
        payload = {"session_id": session_id, "messages": [m.to_dict() for m in messages]}
        self._path_for(session_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_all(self) -> None:
        for session_id in list(self._cache):
            self.save_session(session_id)

