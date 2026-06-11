from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from memory.models import MemoryItem

SELF_MEMORY_TYPES = {
    "identity",
    "preference",
    "goal",
    "project",
    "relationship",
    "habit",
    "instruction",
    "procedure",
    "requested_memory",
}
ALLOWED_PENDING_TAGS = {
    "identity",
    "preference",
    "key_info",
    "health_long_term",
    "requested_memory",
    "correction",
    "procedure",
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
        self.optimization_log_md = self.memory_dir / "OPTIMIZATION_LOG.md"
        self.consolidation_writes_json = self.memory_dir / "consolidation_writes.json"
        self.deleted_jsonl = self.memory_dir / "deleted_memories.jsonl"

    async def initialize(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        defaults = {
            self.memory_md: self._memory_header(),
            self.self_md: self._self_header(),
            self.history_md: "# History\n\n<!-- Timeline events appended by consolidation. -->\n\n",
            self.pending_md: self._pending_header(),
            self.consolidation_log_md: "# Consolidation Log\n\n",
            self.optimization_log_md: "# Optimization Log\n\n",
            self.consolidation_writes_json: json.dumps({"sources": {}}, indent=2) + "\n",
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
            previous_history = self._read_previous_history_sources()
            if previous_history:
                self.write_text(self.history_md, previous_history)

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
        memory_lines = [self._memory_header(), "## Stable Memories\n"]
        self_lines = [self._self_header(), "## User Profile\n"]

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

    def write_recent_context(self, summary: str) -> None:
        sections = self.parse_recent_context(self.read_text(self.recent_context_md))
        sections["compression"] = summary.strip()
        self.write_text(self.recent_context_md, self.render_recent_context(**sections))

    def update_recent_context_sections(
        self,
        *,
        compression: str | None = None,
        ongoing_threads: str | None = None,
        recent_turns: str | None = None,
    ) -> None:
        sections = self.parse_recent_context(self.read_text(self.recent_context_md))
        if compression is not None:
            sections["compression"] = compression.strip()
        if ongoing_threads is not None:
            sections["ongoing_threads"] = ongoing_threads.strip()
        if recent_turns is not None:
            sections["recent_turns"] = recent_turns.strip()
        self.write_text(self.recent_context_md, self.render_recent_context(**sections))

    def append_history(
        self,
        summary: str,
        now: datetime | None = None,
        *,
        title: str | None = None,
        happened_at: str | datetime | None = None,
        source_ref: str = "",
        emotional_weight: int = 0,
    ) -> None:
        if source_ref:
            self.append_history_entry(
                summary,
                happened_at=happened_at or now,
                source_ref=source_ref,
                emotional_weight=emotional_weight,
            )
            return
        timestamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        header = self.read_text(self.history_md)
        if not header.startswith("# History"):
            self.write_text(self.history_md, "# History\n\n")
        title_line = f"### {title}\n\n" if title else ""
        entry = f"## {timestamp}\n\n{title_line}{summary.strip()}\n\n"
        self.append_text(self.history_md, entry)
        self.append_text(self.reflections_md, entry)

    def append_history_entry(
        self,
        summary: str,
        *,
        happened_at: str | datetime | None = None,
        source_ref: str = "",
        emotional_weight: int = 0,
    ) -> bool:
        clean_summary = summary.strip()
        if not clean_summary:
            return False
        clean_source_ref = source_ref.strip()
        if clean_source_ref and self.history_has_source_ref(clean_source_ref):
            return False
        self._ensure_history_header()
        timestamp = self._format_happened_at(happened_at)
        comment = ""
        if clean_source_ref or emotional_weight:
            comment = (
                f"\n<!-- source_ref: {clean_source_ref or 'unknown'} "
                f"emotional_weight: {int(emotional_weight)} -->"
            )
        self.append_text(self.history_md, f"[{timestamp}] {clean_summary}{comment}\n\n")
        return True

    def history_has_source_ref(self, source_ref: str) -> bool:
        clean = source_ref.strip()
        return bool(clean and f"source_ref: {clean}" in self.read_text(self.history_md))

    def append_pending_candidate(
        self,
        tag: str,
        content: str,
        *,
        source_ref: str = "",
        confidence: float | None = None,
        importance: float | None = None,
        metadata: dict[str, object] | None = None,
    ) -> bool:
        clean_tag = tag.strip().lower()
        clean_content = content.strip()
        if clean_tag not in ALLOWED_PENDING_TAGS or not clean_content:
            return False
        if self.pending_has_content(clean_content):
            return False
        self._ensure_pending_header()
        self._remove_pending_none_placeholder()
        metadata = metadata or {}
        meta_parts: list[str] = []
        clean_source_ref = source_ref.strip() or _fallback_pending_source_ref(
            clean_tag,
            clean_content,
        )
        meta_parts.append(f"source_ref: {clean_source_ref}")
        if confidence is not None:
            meta_parts.append(f"confidence: {float(confidence):.2f}")
        if importance is not None:
            meta_parts.append(f"importance: {float(importance):.2f}")
        correction = clean_tag == "correction" or _coerce_bool(metadata.get("correction"))
        requires_review = correction or _coerce_bool(metadata.get("requires_review"))
        if correction:
            meta_parts.append("correction: true")
        if requires_review:
            meta_parts.append("requires_review: true")
        if metadata.get("priority"):
            meta_parts.append(f"priority: {str(metadata['priority']).strip()}")
        if metadata.get("created_at"):
            meta_parts.append(f"created_at: {str(metadata['created_at']).strip()}")
        extra_metadata = {
            str(key): value
            for key, value in metadata.items()
            if key not in {"correction", "requires_review", "priority", "created_at"}
        }
        if extra_metadata:
            meta_parts.append(
                "metadata_json: "
                + json.dumps(
                    extra_metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        comment = f" <!-- {' '.join(meta_parts)} -->" if meta_parts else ""
        self.append_text(self.pending_md, f"- [{clean_tag}] {clean_content}{comment}\n")
        return True

    def pending_has_content(self, content: str) -> bool:
        clean = self._normalize_content(content)
        if not clean:
            return False
        for candidate in self.parse_pending_candidates():
            if self._normalize_content(str(candidate.get("content", ""))) == clean:
                return True
        return False

    def parse_pending_candidates(self) -> list[dict[str, object]]:
        candidates: list[dict[str, object]] = []
        section = "candidates"
        for raw_line in self.read_text(self.pending_md).splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                section_title = line[3:].strip().lower()
                if "requires review" in section_title:
                    section = "requires_review"
                elif "archive" in section_title:
                    section = "processed_archive"
                else:
                    section = "candidates"
                continue
            if section == "processed_archive" or line.startswith("- archived "):
                continue
            if not line.startswith("- [") or "]" not in line:
                continue
            tag, rest = line[3:].split("]", 1)
            clean_tag = tag.strip().lower()
            content, metadata = self._split_pending_content_and_metadata(rest)
            if content:
                metadata_dict = _coerce_metadata_dict(
                    metadata.get("metadata_json", metadata.get("metadata"))
                )
                for key, value in metadata.items():
                    if key not in {
                        "source_ref",
                        "confidence",
                        "importance",
                        "correction",
                        "requires_review",
                        "metadata",
                        "metadata_json",
                    }:
                        metadata_dict.setdefault(key, value)
                requires_review = section == "requires_review" or _coerce_bool(
                    metadata.get("requires_review")
                )
                correction = clean_tag == "correction" or _coerce_bool(
                    metadata.get("correction")
                )
                source_ref = str(metadata.get("source_ref") or "").strip()
                if not source_ref:
                    source_ref = _fallback_pending_source_ref(clean_tag, content)
                candidates.append(
                    {
                        "tag": clean_tag,
                        "content": content,
                        "source_ref": source_ref,
                        "confidence": _coerce_optional_float(metadata.get("confidence")),
                        "importance": _coerce_optional_float(metadata.get("importance")),
                        "correction": correction,
                        "requires_review": requires_review or correction,
                        "metadata": metadata_dict,
                        "section": section,
                    }
                )
        return candidates

    def snapshot_pending(self) -> Path:
        snapshot = self.pending_md.with_suffix(self.pending_md.suffix + ".bak")
        if self.pending_md.exists():
            shutil.copyfile(self.pending_md, snapshot)
        return snapshot

    def restore_pending_snapshot(self) -> bool:
        snapshot = self.pending_md.with_suffix(self.pending_md.suffix + ".bak")
        if not snapshot.exists():
            return False
        self.pending_md.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(snapshot, self.pending_md)
        return True

    def clear_pending_snapshot(self) -> None:
        snapshot = self.pending_md.with_suffix(self.pending_md.suffix + ".bak")
        if snapshot.exists():
            snapshot.unlink()

    def rewrite_pending_candidates(
        self,
        candidates: list[dict[str, object]],
        *,
        archived: list[dict[str, object]] | None = None,
    ) -> None:
        ordinary = [
            candidate
            for candidate in candidates
            if not _coerce_bool(candidate.get("requires_review"))
            and str(candidate.get("section", "")) != "processed_archive"
        ]
        requires_review = [
            candidate
            for candidate in candidates
            if _coerce_bool(candidate.get("requires_review"))
            and str(candidate.get("section", "")) != "processed_archive"
        ]
        existing_archive = self._read_processed_archive_lines()
        new_archive = [
            self._format_pending_candidate(candidate, archived=True)
            for candidate in (archived or [])
        ]

        lines = [self._pending_header(), "## Candidate Long-Term Memories\n"]
        if ordinary:
            lines.extend(self._format_pending_candidate(candidate) for candidate in ordinary)
        else:
            lines.append("- None\n")
        if requires_review:
            lines.append("\n## Requires Review\n")
            lines.extend(self._format_pending_candidate(candidate) for candidate in requires_review)
        archive_lines = existing_archive + new_archive
        if archive_lines:
            lines.append("\n## Processed Archive\n")
            lines.extend(archive_lines)
        self.write_text(self.pending_md, "".join(lines).rstrip() + "\n")

    def has_processed_source_ref(self, source_ref: str) -> bool:
        clean = source_ref.strip()
        if not clean:
            return False
        data = self._read_consolidation_writes()
        sources = data.get("sources", {})
        return isinstance(sources, dict) and clean in sources

    def record_processed_source_ref(
        self,
        source_ref: str,
        *,
        history_count: int = 0,
        pending_count: int = 0,
        skipped: bool = False,
        failed: bool = False,
        error: str = "",
        now: datetime | None = None,
    ) -> None:
        clean = source_ref.strip()
        if not clean:
            return
        data = self._read_consolidation_writes()
        sources = data.setdefault("sources", {})
        if not isinstance(sources, dict):
            sources = {}
            data["sources"] = sources
        sources[clean] = {
            "processed_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "history_count": history_count,
            "pending_count": pending_count,
            "skipped": skipped,
            "failed": failed,
            "error": error,
        }
        self.write_text(
            self.consolidation_writes_json,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )

    def append_consolidation_log(
        self,
        *,
        source_ref: str,
        history_count: int,
        pending_count: int,
        skipped: bool = False,
        failed: bool = False,
        error: str = "",
        now: datetime | None = None,
    ) -> None:
        timestamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        status = "failed" if failed else "skipped" if skipped else "written"
        lines = [
            f"## {timestamp}",
            f"- source_ref: {source_ref or 'unknown'}",
            f"- status: {status}",
            f"- history_entries: {history_count}",
            f"- pending_items: {pending_count}",
        ]
        if error:
            lines.append(f"- error: {error}")
        self.append_text(self.consolidation_log_md, "\n".join(lines) + "\n\n")

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

    def sync_active_from_index(self, items: list[MemoryItem], *, force: bool = False) -> None:
        if (
            force
            or self._is_effectively_empty(self.read_text(self.memory_md))
            or self._is_effectively_empty(self.read_profile())
        ):
            self.render_active_memories(items)
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

    def _read_previous_history_sources(self) -> str:
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
        lines = [
            line.strip()
            for line in stripped.splitlines()
            if line.strip() and not line.strip().startswith("<!--")
        ]
        return len(lines) == 1 and lines[0].startswith("#")

    def _format_memory_line(self, item: MemoryItem) -> str:
        meta = [f"id: {item.id}", f"confidence: {item.confidence:.2f}"]
        if item.source_ref:
            meta.append(f"source_ref: {item.source_ref}")
        if item.status in {"superseded", "deleted"}:
            meta.append(f"status: {item.status}")
        return (
            f"- [{item.type}] {item.text} "
            f"({', '.join(meta)})\n"
        )

    def _format_pending_candidate(
        self,
        candidate: dict[str, object],
        *,
        archived: bool = False,
    ) -> str:
        tag = str(candidate.get("tag") or "requested_memory").strip().lower()
        content = str(candidate.get("content") or "").strip()
        meta_parts: list[str] = []
        source_ref = str(candidate.get("source_ref") or "").strip()
        if not source_ref:
            source_ref = _fallback_pending_source_ref(tag, content)
        meta_parts.append(f"source_ref: {source_ref}")
        confidence = _coerce_optional_float(candidate.get("confidence"))
        if confidence is not None:
            meta_parts.append(f"confidence: {confidence:.2f}")
        importance = _coerce_optional_float(candidate.get("importance"))
        if importance is not None:
            meta_parts.append(f"importance: {importance:.2f}")
        if _coerce_bool(candidate.get("correction")):
            meta_parts.append("correction: true")
        if _coerce_bool(candidate.get("requires_review")):
            meta_parts.append("requires_review: true")
        metadata = _coerce_metadata_dict(candidate.get("metadata"))
        if metadata.get("priority"):
            meta_parts.append(f"priority: {str(metadata.pop('priority')).strip()}")
        if metadata.get("created_at"):
            meta_parts.append(f"created_at: {str(metadata.pop('created_at')).strip()}")
        if metadata:
            meta_parts.append(
                "metadata_json: "
                + json.dumps(
                    metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        comment = f" <!-- {' '.join(meta_parts)} -->" if meta_parts else ""
        prefix = "- archived " if archived else "- "
        return f"{prefix}[{tag}] {content}{comment}\n"

    def _read_processed_archive_lines(self) -> list[str]:
        lines: list[str] = []
        in_archive = False
        for raw_line in self.read_text(self.pending_md).splitlines():
            line = raw_line.rstrip()
            if line.startswith("## "):
                in_archive = "archive" in line[3:].strip().lower()
                continue
            if in_archive and line.strip():
                lines.append(line + "\n")
        return lines

    def _split_pending_content_and_metadata(
        self,
        raw: str,
    ) -> tuple[str, dict[str, object]]:
        text = raw.strip()
        metadata: dict[str, object] = {}
        match = re.search(r"<!--(.*?)-->", text)
        if match:
            metadata = _parse_pending_metadata(match.group(1))
            text = (text[: match.start()] + text[match.end() :]).strip()
        return text, metadata

    def _memory_header(self) -> str:
        return (
            "# Memory\n\n"
            "<!-- Stable long-term memory rendered by the low-frequency MemoryOptimizer. "
            "Ordinary consolidation writes candidates to PENDING.md instead. -->\n\n"
        )

    def _self_header(self) -> str:
        return (
            "# Self\n\n"
            "<!-- Long-term model of the user/agent relationship, service preferences, "
            "projects, habits, and instructions. Rendered from stable profile-like memories. -->\n\n"
        )

    def _pending_header(self) -> str:
        return (
            "# Pending\n\n"
            "<!-- Candidate long-term facts for a future Optimizer. This file is not "
            "regular prompt context. -->\n\n"
        )

    def _ensure_history_header(self) -> None:
        text = self.read_text(self.history_md)
        if not text.startswith("# History"):
            self.write_text(
                self.history_md,
                "# History\n\n<!-- Timeline events appended by consolidation. -->\n\n",
            )

    def _ensure_pending_header(self) -> None:
        text = self.read_text(self.pending_md)
        if not text.startswith("# Pending"):
            self.write_text(self.pending_md, self._pending_header())

    def _remove_pending_none_placeholder(self) -> None:
        text = self.read_text(self.pending_md)
        lines = [line for line in text.splitlines() if line.strip() != "- None"]
        updated = "\n".join(lines).rstrip() + "\n"
        if updated != text:
            self.write_text(self.pending_md, updated)

    def _read_consolidation_writes(self) -> dict[str, Any]:
        raw = self.read_text(self.consolidation_writes_json).strip()
        if not raw:
            return {"sources": {}}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"sources": {}}
        return data if isinstance(data, dict) else {"sources": {}}

    def _format_happened_at(self, value: str | datetime | None) -> str:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        text = str(value or "").strip()
        if text:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return parsed.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                if len(text) >= 16:
                    return text[:16].replace("T", " ")
                if len(text) >= 10:
                    return f"{text[:10]} 00:00"
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _normalize_content(content: str) -> str:
        return " ".join(content.strip().lower().split())


def _parse_pending_metadata(comment: str) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*:\s*|$)", comment.strip()):
        key = match.group(1).strip()
        value = match.group(2).strip()
        if not key:
            continue
        if key in {"metadata", "metadata_json"}:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = {}
            metadata[key] = parsed if isinstance(parsed, dict) else {}
            continue
        if key in {"confidence", "importance"}:
            coerced = _coerce_optional_float(value)
            if coerced is not None:
                metadata[key] = coerced
            continue
        if key in {"correction", "requires_review"}:
            metadata[key] = _coerce_bool(value)
            continue
        metadata[key] = value
    return metadata


def _coerce_optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_metadata_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _fallback_pending_source_ref(tag: str, content: str) -> str:
    digest = hashlib.sha1(f"{tag.strip().lower()}\0{content.strip()}".encode("utf-8")).hexdigest()
    return f"pending:{digest[:16]}"
