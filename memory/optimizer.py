from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import re
from typing import Any

from bootstrap.config import MemoryConfig
from memory.markdown_store import MarkdownMemoryStore
from memory.models import MemoryItem, OptimizationResult
from memory.store import MemoryStore


AUTO_ACTIVE_TAGS = {
    "identity": "identity",
    "preference": "preference",
    "key_info": "key_info",
    "health_long_term": "health_long_term",
    "requested_memory": "requested_memory",
    "procedure": "procedure",
}


class MemoryOptimizer:
    def __init__(
        self,
        store: MemoryStore,
        markdown_store: MarkdownMemoryStore,
        *,
        config: MemoryConfig | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.markdown_store = markdown_store
        self.config = config or MemoryConfig()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    async def optimize(self, *, dry_run: bool = False) -> OptimizationResult:
        if not getattr(self.config, "optimizer_enabled", True):
            return OptimizationResult(summary="optimizer disabled")

        candidates = self.markdown_store.parse_pending_candidates()
        min_pending = int(getattr(self.config, "optimizer_min_pending", 1))
        if len(candidates) < min_pending:
            return OptimizationResult(
                processed=0,
                skipped=len(candidates),
                summary=f"pending candidates below optimizer_min_pending={min_pending}",
            )
        if dry_run:
            return self._dry_run_candidates(candidates)

        snapshot = self.markdown_store.snapshot_pending()
        try:
            result = self._optimize_candidates(candidates)
            self.markdown_store.clear_pending_snapshot()
            return result
        except Exception as exc:
            if snapshot.exists():
                self.markdown_store.restore_pending_snapshot()
            return OptimizationResult(
                ok=False,
                processed=len(candidates),
                summary=f"optimization failed: {exc}",
                errors=[str(exc)],
            )

    def _dry_run_candidates(self, candidates: list[dict[str, object]]) -> OptimizationResult:
        active_by_text = {
            _normalize(item.text): item for item in self.store.read_index() if item.status == "active"
        }
        result = OptimizationResult(processed=len(candidates))
        for candidate in candidates:
            tag = str(candidate.get("tag") or "").strip().lower()
            content = str(candidate.get("content") or "").strip()
            if not tag or not content:
                result.skipped += 1
                continue
            if tag == "correction" or _as_bool(candidate.get("correction")):
                result.requires_review += 1
                continue
            if tag not in AUTO_ACTIVE_TAGS:
                result.skipped += 1
                continue
            duplicate = active_by_text.get(_normalize(content))
            if duplicate is not None:
                result.merged += 1
                result.affected_ids.append(duplicate.id)
            else:
                result.added += 1
        result.summary = (
            f"dry_run processed={result.processed}, added={result.added}, "
            f"merged={result.merged}, skipped={result.skipped}, "
            f"requires_review={result.requires_review}"
        )
        return result

    def _optimize_candidates(self, candidates: list[dict[str, object]]) -> OptimizationResult:
        now = self.now_fn()
        index = self.store.read_index()
        active_by_text = {
            _normalize(item.text): item for item in index if item.status == "active"
        }
        result = OptimizationResult(processed=len(candidates))
        remaining: list[dict[str, object]] = []
        archived: list[dict[str, object]] = []
        log_lines = [f"## {now.isoformat(timespec='seconds')}\n"]

        for candidate in candidates:
            tag = str(candidate.get("tag") or "").strip().lower()
            content = str(candidate.get("content") or "").strip()
            if not tag or not content:
                result.skipped += 1
                continue

            if tag == "correction" or _as_bool(candidate.get("correction")):
                review_candidate = dict(candidate)
                review_candidate["tag"] = tag or "correction"
                review_candidate["correction"] = True
                review_candidate["requires_review"] = True
                review_candidate["section"] = "requires_review"
                remaining.append(review_candidate)
                result.requires_review += 1
                log_lines.append(f"- requires_review: [correction] {content}\n")
                continue

            memory_type = AUTO_ACTIVE_TAGS.get(tag)
            if memory_type is None:
                remaining.append(candidate)
                result.skipped += 1
                log_lines.append(f"- skipped: [{tag}] {content}\n")
                continue

            normalized = _normalize(content)
            duplicate = active_by_text.get(normalized)
            if duplicate is not None:
                self._merge_candidate(duplicate, candidate, tag=tag, now=now)
                result.merged += 1
                result.affected_ids.append(duplicate.id)
                archived.append(candidate)
                log_lines.append(f"- merged into {duplicate.id}: [{tag}] {content}\n")
                continue

            item = self._candidate_to_memory_item(candidate, memory_type=memory_type, tag=tag, now=now)
            index.append(item)
            active_by_text[normalized] = item
            result.added += 1
            result.affected_ids.append(item.id)
            archived.append(candidate)
            log_lines.append(f"- added {item.id}: [{tag}] {content}\n")

        self.store.write_index(index)
        self.markdown_store.render_active_memories(index)

        archive_processed = bool(getattr(self.config, "optimizer_archive_processed", True))
        archive_payload = archived if archive_processed else []
        self.markdown_store.rewrite_pending_candidates(remaining, archived=archive_payload)
        if archive_processed:
            result.archived = len(archived)

        result.summary = (
            f"processed={result.processed}, added={result.added}, merged={result.merged}, "
            f"skipped={result.skipped}, requires_review={result.requires_review}, "
            f"archived={result.archived}"
        )
        log_lines.append(f"- summary: {result.summary}\n\n")
        self.markdown_store.append_text(self.markdown_store.optimization_log_md, "".join(log_lines))
        return result

    def _candidate_to_memory_item(
        self,
        candidate: dict[str, object],
        *,
        memory_type: str,
        tag: str,
        now: datetime,
    ) -> MemoryItem:
        metadata = _candidate_metadata(candidate)
        source_ref = str(candidate.get("source_ref") or "").strip()
        return MemoryItem(
            type=memory_type,  # type: ignore[arg-type]
            text=str(candidate.get("content") or "").strip(),
            tags=sorted({tag, *[str(item) for item in metadata.pop("tags", [])]})
            if isinstance(metadata.get("tags"), list)
            else [tag],
            importance=_candidate_float(candidate, "importance", default=0.5),
            confidence=_candidate_float(candidate, "confidence", default=0.5),
            source="pending:optimizer",
            source_ref=source_ref,
            happened_at=_candidate_datetime(candidate, "happened_at", metadata),
            emotional_weight=_candidate_int(
                candidate,
                "emotional_weight",
                metadata,
                default=0,
            ),
            metadata=metadata,
            created_at=now,
            updated_at=now,
            status="active",
        )

    def _merge_candidate(
        self,
        item: MemoryItem,
        candidate: dict[str, object],
        *,
        tag: str,
        now: datetime,
    ) -> None:
        item.importance = max(item.importance, _candidate_float(candidate, "importance", default=0.5))
        item.confidence = max(item.confidence, _candidate_float(candidate, "confidence", default=0.5))
        item.updated_at = now
        item.tags = sorted(set(item.tags) | {tag})
        candidate_metadata = _candidate_metadata(candidate)
        item.metadata.update(candidate_metadata)

        source_ref = str(candidate.get("source_ref") or "").strip()
        if not source_ref:
            return
        if not item.source_ref:
            item.source_ref = source_ref
        source_refs = item.metadata.get("source_refs", [])
        if not isinstance(source_refs, list):
            source_refs = []
        merged_refs = {str(ref) for ref in source_refs if str(ref).strip()}
        if item.source_ref:
            merged_refs.add(item.source_ref)
        merged_refs.add(source_ref)
        item.metadata["source_refs"] = sorted(merged_refs)


def _candidate_metadata(candidate: dict[str, object]) -> dict[str, Any]:
    metadata = candidate.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    return {}


def _candidate_float(candidate: dict[str, object], key: str, *, default: float) -> float:
    value = candidate.get(key)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _candidate_int(
    candidate: dict[str, object],
    key: str,
    metadata: dict[str, Any],
    *,
    default: int,
) -> int:
    value = candidate.get(key, metadata.get(key))
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _candidate_datetime(
    candidate: dict[str, object],
    key: str,
    metadata: dict[str, Any],
) -> datetime | None:
    value = candidate.get(key, metadata.get(key))
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())
