from __future__ import annotations

from pathlib import Path
import json

from memory.models import MemoryItem


class MemoryStore:
    def __init__(self, memory_dir: str | Path) -> None:
        self.memory_dir = Path(memory_dir)
        self.memory_md = self.memory_dir / "MEMORY.md"
        self.profile_md = self.memory_dir / "PROFILE.md"
        self.recent_context_md = self.memory_dir / "RECENT_CONTEXT.md"
        self.pending_jsonl = self.memory_dir / "PENDING_MEMORIES.jsonl"
        self.index_json = self.memory_dir / "MEMORY_INDEX.json"
        self.reflections_md = self.memory_dir / "REFLECTIONS.md"
        self.consolidation_log_md = self.memory_dir / "CONSOLIDATION_LOG.md"
        self.deleted_jsonl = self.memory_dir / "deleted_memories.jsonl"

    async def initialize(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        defaults = {
            self.memory_md: "# 长期记忆\n\n",
            self.profile_md: "# 用户画像\n\n",
            self.recent_context_md: "# 近期上下文\n\n",
            self.pending_jsonl: "",
            self.index_json: "[]\n",
            self.reflections_md: "# 反思记录\n\n",
            self.consolidation_log_md: "# 记忆整理日志\n\n",
            self.deleted_jsonl: "",
        }
        for path, content in defaults.items():
            if not path.exists():
                path.write_text(content, encoding="utf-8")

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def append_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(content)

    def read_index(self) -> list[MemoryItem]:
        if not self.index_json.exists():
            return []
        data = json.loads(self.index_json.read_text(encoding="utf-8") or "[]")
        return [MemoryItem.from_dict(item) for item in data]

    def write_index(self, items: list[MemoryItem]) -> None:
        self.write_text(
            self.index_json,
            json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2) + "\n",
        )

    def append_pending(self, item: MemoryItem) -> None:
        self.append_text(self.pending_jsonl, json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

    def read_pending(self) -> list[MemoryItem]:
        if not self.pending_jsonl.exists():
            return []
        items: list[MemoryItem] = []
        for line in self.pending_jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append(MemoryItem.from_dict(json.loads(line)))
        return items

    def clear_pending(self) -> None:
        self.write_text(self.pending_jsonl, "")

    def append_deleted(self, item: MemoryItem, reason: str) -> None:
        payload = item.to_dict()
        payload["delete_reason"] = reason
        self.append_text(self.deleted_jsonl, json.dumps(payload, ensure_ascii=False) + "\n")
