from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from memory.models import MemoryItem

SELF_MEMORY_TYPES = {
    "preference",
    "goal",
    "project",
    "relationship",
    "habit",
    "instruction",
}

RECENT_CONTEXT_HEADER = "# Recent Context"
RECENT_CONTEXT_SECTIONS = (
    "Compression",
    "Ongoing Threads",
    "Recent Turns",
)


class MarkdownMemoryStore:
    def __init__(self, memory_dir: str | Path) -> None:
        self.memory_dir = Path(memory_dir)
        self.memory_md = self.memory_dir / "MEMORY.md"
        self.self_md = self.memory_dir / "SELF.md"
        self.history_md = self.memory_dir / "HISTORY.md"
        self.recent_context_md = self.memory_dir / "RECENT_CONTEXT.md"
        self.pending_md = self.memory_dir / "PENDING.md"
        self.profile_md = self.memory_dir / "PROFILE.md"
        self.pending_jsonl = self.memory_dir / "PENDING_MEMORIES.jsonl"
        self.index_json = self.memory_dir / "MEMORY_INDEX.json"
        self.reflections_md = self.memory_dir / "REFLECTIONS.md"
        self.consolidation_log_md = self.memory_dir / "CONSOLIDATION_LOG.md"
        self.deleted_jsonl = self.memory_dir / "deleted_memories.jsonl"

    async def initialize(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        defaults = {
            self.memory_md: "# Memory\n\n",
            self.self_md: "# Self\n\n",
            self.history_md: "# History\n\n",
            self.pending_md: "# Pending\n\n",
        }
        for path, content in defaults.items():
            if not path.exists():
                path.write_text(content, encoding="utf-8")

        if self.profile_md.exists() and self._is_effectively_empty(self.read_text(self.self_md)):
            self.write_text(self.self_md, self.read_text(self.profile_md))
        elif self.self_md.exists() and (
            not self.profile_md.exists()
            or self._is_effectively_empty(self.read_text(self.profile_md))
        ):
            self.write_text(self.profile_md, self.read_text(self.self_md))

        if not self.recent_context_md.exists():
            self.write_text(self.recent_context_md, self.render_recent_context())
        else:
            self.write_text(
                self.recent_context_md,
                self.normalize_recent_context(self.read_text(self.recent_context_md)),
            )

        if self._is_effectively_empty(self.read_text(self.history_md)):
            legacy_history = self._read_legacy_history()
            if legacy_history:
                self.write_text(self.history_md, legacy_history)

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def append_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(content)

    def read_profile(self) -> str:
        self_text = self.read_text(self.self_md)
        if self_text and not self._is_effectively_empty(self_text):
            return self_text
        profile_text = self.read_text(self.profile_md)
        if profile_text:
            return profile_text
        return "# Self\n\n"

    def read_recent_context(self) -> str:
        text = self.read_text(self.recent_context_md)
        normalized = self.normalize_recent_context(text)
        if normalized != text:
            self.write_text(self.recent_context_md, normalized)
        return normalized

    def render_active_memories(self, items: list[MemoryItem]) -> None:
        active = sorted(
            (item for item in items if item.status == "active"),
            key=lambda item: (-item.importance, item.type, item.text.lower(), item.id),
        )
        memory_lines = ["# Memory\n\n", "## Stable Memories\n"]
        self_lines = ["# Self\n\n", "## User Profile\n"]

        for item in active:
            line = self._format_memory_line(item)
            memory_lines.append(line)
            if item.type in SELF_MEMORY_TYPES:
                self_lines.append(line)

        if len(memory_lines) == 2:
            memory_lines.append("- None\n")
        if len(self_lines) == 2:
            self_lines.append("- None\n")

        memory_content = "".join(memory_lines) + "\n"
        self_content = "".join(self_lines) + "\n"
        self.write_text(self.memory_md, memory_content)
        self.write_text(self.self_md, self_content)
        self.write_text(self.profile_md, self_content)

    def render_pending_memories(self, items: list[MemoryItem]) -> None:
        pending_items = sorted(
            (item for item in items if item.status == "pending"),
            key=lambda item: (-item.importance, item.type, item.text.lower(), item.id),
        )
        lines = ["# Pending\n\n", "## Candidate Long-Term Memories\n"]
        if pending_items:
            lines.extend(self._format_memory_line(item) for item in pending_items)
        else:
            lines.append("- None\n")
        self.write_text(self.pending_md, "".join(lines) + "\n")

    def write_recent_context(self, summary: str) -> None:
        sections = self.parse_recent_context(self.read_text(self.recent_context_md))
        sections["compression"] = summary.strip()
        self.write_text(self.recent_context_md, self.render_recent_context(**sections))

    def append_history(
        self,
        summary: str,
        now: datetime | None = None,
        *,
        title: str | None = None,
    ) -> None:
        timestamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        header = self.read_text(self.history_md)
        if not header.startswith("# History"):
            self.write_text(self.history_md, "# History\n\n")
        title_line = f"### {title}\n\n" if title else ""
        entry = f"## {timestamp}\n\n{title_line}{summary.strip()}\n\n"
        self.append_text(self.history_md, entry)
        self.append_text(self.reflections_md, entry)

    def append_history_lines(
        self,
        lines: list[str],
        now: datetime | None = None,
        *,
        title: str | None = None,
    ) -> None:
        cleaned = [line.rstrip() for line in lines if line.strip()]
        if not cleaned:
            cleaned = ["- No notable events."]
        self.append_history("\n".join(cleaned), now=now, title=title)

    def sync_from_legacy(
        self,
        items: list[MemoryItem],
        pending_items: list[MemoryItem],
        *,
        force: bool = False,
    ) -> None:
        if force or self._is_effectively_empty(self.read_text(self.memory_md)):
            self.render_active_memories(items)
        if force or self._is_effectively_empty(self.read_profile()):
            self.render_active_memories(items)
        if force or self._is_effectively_empty(self.read_text(self.pending_md)):
            self.render_pending_memories(pending_items)
        if force:
            self.write_text(
                self.recent_context_md,
                self.normalize_recent_context(self.read_text(self.recent_context_md)),
            )

    def render_recent_context(
        self,
        compression: str = "",
        ongoing_threads: str = "",
        recent_turns: str = "",
    ) -> str:
        sections = {
            "Compression": compression.strip(),
            "Ongoing Threads": ongoing_threads.strip(),
            "Recent Turns": recent_turns.strip(),
        }
        lines = [RECENT_CONTEXT_HEADER, ""]
        for section_name in RECENT_CONTEXT_SECTIONS:
            lines.append(f"## {section_name}")
            section_body = sections[section_name]
            if section_body:
                lines.append(section_body)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def parse_recent_context(self, text: str) -> dict[str, str]:
        stripped = text.strip()
        if self._is_effectively_empty(stripped):
            return {
                "compression": "",
                "ongoing_threads": "",
                "recent_turns": "",
            }

        if not stripped.startswith(RECENT_CONTEXT_HEADER):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("#"):
                stripped = "\n".join(lines[1:]).strip()
            return {
                "compression": stripped,
                "ongoing_threads": "",
                "recent_turns": "",
            }

        raw_sections = {
            "Compression": "",
            "Ongoing Threads": "",
            "Recent Turns": "",
        }
        current_section: str | None = None
        buffer: list[str] = []

        def flush() -> None:
            nonlocal buffer, current_section
            if current_section is None:
                buffer = []
                return
            raw_sections[current_section] = "\n".join(buffer).strip()
            buffer = []

        for raw_line in stripped.splitlines()[1:]:
            line = raw_line.rstrip()
            if line.startswith("## "):
                flush()
                section_name = line[3:].strip()
                current_section = section_name if section_name in raw_sections else None
                continue
            if current_section is not None:
                buffer.append(line)
        flush()
        return {
            "compression": raw_sections["Compression"],
            "ongoing_threads": raw_sections["Ongoing Threads"],
            "recent_turns": raw_sections["Recent Turns"],
        }

    def normalize_recent_context(self, text: str) -> str:
        sections = self.parse_recent_context(text)
        return self.render_recent_context(
            compression=sections["compression"],
            ongoing_threads=sections["ongoing_threads"],
            recent_turns=sections["recent_turns"],
        )

    def _read_legacy_history(self) -> str:
        sections: list[str] = []
        reflections = self.read_text(self.reflections_md).strip()
        if reflections and not self._is_effectively_empty(reflections):
            sections.append(reflections)
        consolidation = self.read_text(self.consolidation_log_md).strip()
        if consolidation and not self._is_effectively_empty(consolidation):
            sections.append(consolidation)
        if not sections:
            return ""
        return "# History\n\n" + "\n\n".join(sections) + "\n"

    def _is_effectively_empty(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        return len(lines) == 1 and lines[0].startswith("#")

    def _format_memory_line(self, item: MemoryItem) -> str:
        return (
            f"- [{item.type}] {item.text} "
            f"(id: {item.id}, confidence: {item.confidence:.2f})\n"
        )
