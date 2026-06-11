from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from agent.llm import LLMProvider
from bootstrap.config import MemoryConfig
from memory.markdown_store import MarkdownMemoryStore
from memory.merge_resolver import (
    LLMMemoryMergeResolver,
    MergeCandidate,
    MergeDecision,
)
from memory.memory2_store import SQLiteMemory2Store
from memory.models import MemoryItem, OptimizationResult
from memory.store import MemoryStore
from memory.vector_store import VectorMemoryRecord, VectorMemoryStore


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
        memory2_store: SQLiteMemory2Store | None = None,
        vector_store: VectorMemoryStore | None = None,
        llm_provider: LLMProvider | None = None,
        merge_resolver: LLMMemoryMergeResolver | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.markdown_store = markdown_store
        self.config = config or MemoryConfig()
        self.memory2_store = memory2_store
        self.vector_store = vector_store
        self.llm_provider = llm_provider
        self.merge_resolver = merge_resolver
        if self.merge_resolver is None and bool(getattr(self.config, "llm_merge_enabled", False)):
            self.merge_resolver = LLMMemoryMergeResolver(
                config=self.config,
                provider=llm_provider,
            )
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
            result = await self._optimize_candidates(candidates)
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
            explicit_supersede_ids = _coerce_id_list(
                _candidate_metadata(candidate).get("supersedes")
            )
            needs_review = _requires_review(candidate, tag)
            if (tag == "correction" or _as_bool(candidate.get("correction"))) and not explicit_supersede_ids:
                needs_review = True
            if needs_review:
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
            f"merged={result.merged}, superseded={result.superseded}, skipped={result.skipped}, "
            f"requires_review={result.requires_review}"
        )
        return result

    async def _optimize_candidates(
        self,
        candidates: list[dict[str, object]],
    ) -> OptimizationResult:
        now = self.now_fn()
        index = self.store.read_index()
        active_by_text = {
            _normalize(item.text): item for item in index if item.status == "active"
        }
        result = OptimizationResult(processed=len(candidates))
        remaining: list[dict[str, object]] = []
        archived: list[dict[str, object]] = []
        changed_items: list[MemoryItem] = []
        log_lines = [f"## {now.isoformat(timespec='seconds')}\n"]

        for candidate in candidates:
            tag = str(candidate.get("tag") or "").strip().lower()
            content = str(candidate.get("content") or "").strip()
            if not tag or not content:
                result.skipped += 1
                continue

            explicit_supersede_ids = _candidate_supersede_ids(candidate, index)
            memory_type = AUTO_ACTIVE_TAGS.get(tag)
            if memory_type is None and explicit_supersede_ids and tag == "correction":
                memory_type = "correction"
            if memory_type is None and tag == "correction":
                memory_type = "correction"
            if memory_type is None:
                remaining.append(candidate)
                result.skipped += 1
                log_lines.append(f"- skipped: [{tag}] {content}\n")
                continue

            exact_duplicate = await self._find_exact_duplicate(candidate, index, active_by_text)
            if exact_duplicate is not None:
                await self._merge_candidate(exact_duplicate, candidate, tag=tag, now=now)
                result.merged += 1
                result.affected_ids.append(exact_duplicate.id)
                changed_items.append(exact_duplicate)
                archived.append(candidate)
                log_lines.append(f"- merged into {exact_duplicate.id}: [{tag}] {content}\n")
                continue

            procedure_supersede_ids = _procedure_supersede_ids(
                candidate,
                index,
                tag=tag,
                memory_type=memory_type,
            )
            supersede_ids = _dedupe_ids([*explicit_supersede_ids, *procedure_supersede_ids])
            llm_decision, merge_candidate, fallback_reason = await self._resolve_llm_candidate(
                candidate,
                index,
                explicit_supersede_ids=supersede_ids,
            )
            if merge_candidate is not None and fallback_reason:
                self._append_llm_merge_log(
                    merge_candidate,
                    None,
                    applied=False,
                    fallback=fallback_reason,
                )
            if llm_decision is not None and merge_candidate is not None:
                applied = await self._apply_merge_decision(
                    llm_decision,
                    merge_candidate,
                    candidate,
                    index,
                    active_by_text,
                    remaining=remaining,
                    archived=archived,
                    changed_items=changed_items,
                    result=result,
                    memory_type=memory_type,
                    tag=tag,
                    now=now,
                )
                self._append_llm_merge_log(
                    merge_candidate,
                    llm_decision,
                    applied=applied,
                    fallback="" if applied else "decision was not applied",
                )
                if applied:
                    log_lines.append(
                        f"- llm_{llm_decision.action}: [{tag}] {content}\n"
                    )
                    continue

            if _requires_review(candidate, tag):
                review_candidate = self._review_candidate(candidate, tag=tag)
                remaining.append(review_candidate)
                result.requires_review += 1
                log_lines.append(f"- requires_review: [{tag}] {content}\n")
                continue

            if supersede_ids:
                item = self._candidate_to_memory_item(
                    candidate,
                    memory_type=memory_type,
                    tag=tag,
                    now=now,
                )
                item.supersedes = supersede_ids
                index.append(item)
                active_by_text[_normalize(content)] = item
                result.added += 1
                result.superseded += await self._mark_superseded(
                    index,
                    supersede_ids,
                    new_item=item,
                    changed_items=changed_items,
                )
                result.affected_ids.append(item.id)
                changed_items.append(item)
                archived.append(candidate)
                log_lines.append(
                    f"- superseded {', '.join(supersede_ids)} with {item.id}: [{tag}] {content}\n"
                )
                continue

            normalized = _normalize(content)
            duplicate = await self._find_duplicate(candidate, index, active_by_text)
            if duplicate is not None:
                await self._merge_candidate(duplicate, candidate, tag=tag, now=now)
                result.merged += 1
                result.affected_ids.append(duplicate.id)
                changed_items.append(duplicate)
                archived.append(candidate)
                log_lines.append(f"- merged into {duplicate.id}: [{tag}] {content}\n")
                continue

            item = self._candidate_to_memory_item(candidate, memory_type=memory_type, tag=tag, now=now)
            index.append(item)
            active_by_text[normalized] = item
            result.added += 1
            result.affected_ids.append(item.id)
            changed_items.append(item)
            archived.append(candidate)
            log_lines.append(f"- added {item.id}: [{tag}] {content}\n")

        self.store.write_index(index)
        self.markdown_store.render_active_memories(index)
        await self._sync_changed_items(changed_items, result)

        archive_processed = bool(getattr(self.config, "optimizer_archive_processed", True))
        archive_payload = archived if archive_processed else []
        self.markdown_store.rewrite_pending_candidates(remaining, archived=archive_payload)
        if archive_processed:
            result.archived = len(archived)

        result.summary = (
            f"processed={result.processed}, added={result.added}, merged={result.merged}, "
            f"superseded={result.superseded}, skipped={result.skipped}, requires_review={result.requires_review}, "
            f"archived={result.archived}"
        )
        log_lines.append(f"- summary: {result.summary}\n\n")
        self.markdown_store.append_text(self.markdown_store.optimization_log_md, "".join(log_lines))
        return result

    async def _resolve_llm_candidate(
        self,
        candidate: dict[str, object],
        index: list[MemoryItem],
        *,
        explicit_supersede_ids: list[str],
    ) -> tuple[MergeDecision | None, MergeCandidate | None, str]:
        if not bool(getattr(self.config, "llm_merge_enabled", False)):
            return None, None, ""
        merge_candidate = await self._candidate_to_merge_candidate(
            candidate,
            index,
            explicit_supersede_ids=explicit_supersede_ids,
        )
        if self.merge_resolver is None:
            return None, merge_candidate, "llm merge enabled but resolver/provider is unavailable"
        try:
            decision = await self.merge_resolver.resolve(merge_candidate)
        except Exception as exc:
            return None, merge_candidate, f"llm merge resolver failed: {exc}"
        if decision is None:
            return None, merge_candidate, "llm merge resolver returned no decision"
        return decision, merge_candidate, ""

    async def _candidate_to_merge_candidate(
        self,
        candidate: dict[str, object],
        index: list[MemoryItem],
        *,
        explicit_supersede_ids: list[str],
    ) -> MergeCandidate:
        tag = str(candidate.get("tag") or "").strip().lower()
        content = str(candidate.get("content") or "").strip()
        metadata = _candidate_metadata(candidate)
        source_ref = str(candidate.get("source_ref") or "").strip()
        return MergeCandidate(
            pending_id=source_ref or _memory2_content_hash(content)[:16],
            source_ref=source_ref,
            tag=tag,
            content=content,
            confidence=_candidate_float(candidate, "confidence", default=0.0),
            importance=_candidate_float(candidate, "importance", default=0.0),
            metadata=metadata,
            similar_active=await self._similar_active_memories(
                content,
                index,
                explicit_supersede_ids=explicit_supersede_ids,
            ),
            explicit_supersedes=list(explicit_supersede_ids),
        )

    async def _similar_active_memories(
        self,
        content: str,
        index: list[MemoryItem],
        *,
        explicit_supersede_ids: list[str],
    ) -> list[dict[str, Any]]:
        max_candidates = max(int(getattr(self.config, "llm_merge_max_candidates", 5)), 1)
        active_by_id = {item.id: item for item in index if item.status == "active"}
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_item(item: MemoryItem, score: float) -> None:
            if item.id in seen or item.status != "active":
                return
            seen.add(item.id)
            rows.append(_item_to_similar_dict(item, score=score))

        for memory_id in explicit_supersede_ids:
            item = active_by_id.get(memory_id)
            if item is not None:
                add_item(item, 1.0)

        if self.vector_store is not None and self.vector_store.is_enabled() and content:
            try:
                matches = await self.vector_store.search(
                    content,
                    top_k=max_candidates,
                    score_threshold=0.0,
                )
            except Exception:
                matches = []
            for match in matches:
                item = active_by_id.get(match.record.item_id)
                if item is not None:
                    add_item(item, float(match.score))

        scored = [
            (_text_similarity(content, item.text), item)
            for item in active_by_id.values()
            if item.id not in seen
        ]
        scored = [(score, item) for score, item in scored if score > 0.0]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        for score, item in scored:
            if len(rows) >= max_candidates:
                break
            add_item(item, score)
        return rows[:max_candidates]

    async def _apply_merge_decision(
        self,
        decision: MergeDecision,
        merge_candidate: MergeCandidate,
        candidate: dict[str, object],
        index: list[MemoryItem],
        active_by_text: dict[str, MemoryItem],
        *,
        remaining: list[dict[str, object]],
        archived: list[dict[str, object]],
        changed_items: list[MemoryItem],
        result: OptimizationResult,
        memory_type: str,
        tag: str,
        now: datetime,
    ) -> bool:
        valid_targets = {item.id: item for item in index if item.status == "active"}
        target_items = [
            valid_targets[target_id]
            for target_id in decision.target_ids
            if target_id in valid_targets
        ]

        if decision.action == "requires_review":
            remaining.append(self._review_candidate(candidate, tag=tag, decision=decision))
            result.requires_review += 1
            return True

        if decision.action == "skip":
            threshold = float(getattr(self.config, "llm_merge_confidence_threshold", 0.75))
            if decision.confidence < threshold or not decision.reason.strip():
                review = MergeDecision(
                    action="requires_review",
                    target_ids=list(decision.target_ids),
                    confidence=decision.confidence,
                    reason=decision.reason or "skip was not confident enough",
                    sensitive=decision.sensitive,
                    metadata={**decision.metadata, "suggested_action": "skip"},
                )
                remaining.append(self._review_candidate(candidate, tag=tag, decision=review))
                result.requires_review += 1
                return True
            archived.append(candidate)
            result.skipped += 1
            return True

        if decision.action == "add":
            item_candidate = _candidate_with_decision(candidate, decision)
            item = self._candidate_to_memory_item(
                item_candidate,
                memory_type=memory_type,
                tag=tag,
                now=now,
            )
            index.append(item)
            active_by_text[_normalize(item.text)] = item
            changed_items.append(item)
            archived.append(candidate)
            result.added += 1
            result.affected_ids.append(item.id)
            return True

        if decision.action in {"reinforce", "merge"}:
            if not target_items:
                review = MergeDecision(
                    action="requires_review",
                    target_ids=list(decision.target_ids),
                    confidence=decision.confidence,
                    reason=decision.reason or "target memory was not found",
                    sensitive=decision.sensitive,
                    metadata={**decision.metadata, "suggested_action": decision.action},
                )
                remaining.append(self._review_candidate(candidate, tag=tag, decision=review))
                result.requires_review += 1
                return True
            target = target_items[0]
            merge_payload = _candidate_with_decision(candidate, decision)
            if decision.action == "merge" and decision.new_summary:
                active_by_text.pop(_normalize(target.text), None)
                target.text = decision.new_summary
                active_by_text[_normalize(target.text)] = target
            await self._merge_candidate(target, merge_payload, tag=tag, now=now)
            if decision.new_body:
                target.metadata["body"] = decision.new_body
            target.metadata["llm_merge_reason"] = decision.reason
            target.metadata["llm_merge_confidence"] = decision.confidence
            changed_items.append(target)
            archived.append(candidate)
            result.merged += 1
            result.affected_ids.append(target.id)
            return True

        if decision.action == "supersede":
            explicit_targets = set(merge_candidate.explicit_supersedes)
            auto_allowed = bool(getattr(self.config, "llm_merge_allow_auto_supersede", False))
            if (
                not target_items
                or decision.sensitive
                or (not auto_allowed and not explicit_targets)
            ):
                review = MergeDecision(
                    action="requires_review",
                    target_ids=list(decision.target_ids),
                    confidence=decision.confidence,
                    reason=decision.reason or "supersede requires review",
                    sensitive=decision.sensitive,
                    metadata={**decision.metadata, "suggested_action": "supersede"},
                )
                remaining.append(self._review_candidate(candidate, tag=tag, decision=review))
                result.requires_review += 1
                return True
            item_candidate = _candidate_with_decision(candidate, decision)
            item = self._candidate_to_memory_item(
                item_candidate,
                memory_type=memory_type,
                tag=tag,
                now=now,
            )
            item.supersedes = [target.id for target in target_items]
            index.append(item)
            active_by_text[_normalize(item.text)] = item
            changed_items.append(item)
            result.added += 1
            result.superseded += await self._mark_superseded(
                index,
                item.supersedes,
                new_item=item,
                changed_items=changed_items,
            )
            result.affected_ids.append(item.id)
            archived.append(candidate)
            return True

        return False

    def _review_candidate(
        self,
        candidate: dict[str, object],
        *,
        tag: str,
        decision: MergeDecision | None = None,
    ) -> dict[str, object]:
        review_candidate = dict(candidate)
        review_candidate["tag"] = tag
        if tag == "correction" or _as_bool(candidate.get("correction")):
            review_candidate["correction"] = True
        review_candidate["requires_review"] = True
        review_candidate["section"] = "requires_review"
        if decision is not None:
            metadata = _candidate_metadata(review_candidate)
            metadata.update(
                {
                    "llm_action": decision.action,
                    "llm_target_ids": list(decision.target_ids),
                    "llm_confidence": decision.confidence,
                    "llm_reason": decision.reason,
                    "sensitive": decision.sensitive,
                }
            )
            if decision.metadata:
                metadata["merge_recommendation"] = dict(decision.metadata)
            review_candidate["metadata"] = metadata
        return review_candidate

    def _append_llm_merge_log(
        self,
        candidate: MergeCandidate,
        decision: MergeDecision | None,
        *,
        applied: bool,
        fallback: str = "",
    ) -> None:
        now = self.now_fn().isoformat(timespec="seconds")
        action = decision.action if decision is not None else "fallback"
        target_ids = decision.target_ids if decision is not None else []
        confidence = decision.confidence if decision is not None else 0.0
        sensitive = decision.sensitive if decision is not None else False
        reason = decision.reason if decision is not None else fallback
        similar_ids = [str(item.get("id") or "") for item in candidate.similar_active]
        lines = [
            f"## {now}",
            f"- source_ref: {candidate.source_ref or candidate.pending_id or 'unknown'}",
            f"- pending: {self._redact_log_text(_truncate(candidate.content, 300))}",
            f"- similar_ids: {', '.join(similar_ids) or 'none'}",
            f"- action: {action}",
            f"- target_ids: {', '.join(target_ids) or 'none'}",
            f"- confidence: {confidence:.2f}",
            f"- sensitive: {str(bool(sensitive)).lower()}",
            f"- reason: {self._redact_log_text(_truncate(reason, 500))}",
            f"- applied: {str(bool(applied)).lower()}",
        ]
        if fallback:
            lines.append(f"- fallback: {self._redact_log_text(_truncate(fallback, 500))}")
        self.markdown_store.append_text(self._llm_merge_log_path(), "\n".join(lines) + "\n\n")

    def _llm_merge_log_path(self) -> Path:
        raw_path = Path(getattr(self.config, "llm_merge_log_path", ".collie/memory/LLM_MERGE_LOG.md"))
        if raw_path.is_absolute():
            return raw_path
        return self.markdown_store.memory_dir.parent / raw_path

    def _redact_log_text(self, text: str) -> str:
        redacted = str(text)
        provider_key = str(getattr(self.llm_provider, "api_key", "") or "")
        for secret in (
            getattr(self.config, "memory_server_api_key", ""),
            getattr(self.config, "embedding_api_key", ""),
            provider_key,
        ):
            clean = str(secret or "")
            if clean:
                redacted = redacted.replace(clean, "[redacted]")
        return redacted

    async def _sync_changed_items(
        self,
        items: list[MemoryItem],
        result: OptimizationResult,
    ) -> None:
        if not items:
            return
        for item in items:
            try:
                if self.vector_store is not None and self.vector_store.is_enabled():
                    await self.vector_store.upsert(_item_to_vector_record(item))
                elif self.memory2_store is not None:
                    await self.memory2_store.upsert_item(item)
            except Exception as exc:
                result.errors.append(str(exc))

    async def _find_duplicate(
        self,
        candidate: dict[str, object],
        index: list[MemoryItem],
        active_by_text: dict[str, MemoryItem],
    ) -> MemoryItem | None:
        content = str(candidate.get("content") or "").strip()
        if not content:
            return None
        normalized = _normalize(content)
        duplicate = active_by_text.get(normalized)
        if duplicate is not None and duplicate.status == "active":
            return duplicate

        if self.memory2_store is not None:
            try:
                row = await self.memory2_store.find_by_content_hash(
                    _memory2_content_hash(content)
                )
            except Exception:
                row = None
            if row is not None:
                by_id = {item.id: item for item in index}
                item = by_id.get(str(row.get("id") or ""))
                if item is not None and item.status == "active":
                    return item

        if self.vector_store is not None and self.vector_store.is_enabled():
            try:
                matches = await self.vector_store.search(
                    content,
                    top_k=1,
                    score_threshold=float(
                        getattr(self.config, "semantic_dedup_threshold", 0.88)
                    ),
                )
            except Exception:
                matches = []
            if matches:
                by_id = {item.id: item for item in index}
                item = by_id.get(matches[0].record.item_id)
                if item is not None and item.status == "active":
                    return item

        return duplicate

    async def _find_exact_duplicate(
        self,
        candidate: dict[str, object],
        index: list[MemoryItem],
        active_by_text: dict[str, MemoryItem],
    ) -> MemoryItem | None:
        content = str(candidate.get("content") or "").strip()
        if not content:
            return None
        duplicate = active_by_text.get(_normalize(content))
        if duplicate is not None and duplicate.status == "active":
            return duplicate
        if self.memory2_store is None:
            return None
        try:
            row = await self.memory2_store.find_by_content_hash(
                _memory2_content_hash(content)
            )
        except Exception:
            return None
        if row is None:
            return None
        by_id = {item.id: item for item in index}
        item = by_id.get(str(row.get("id") or ""))
        return item if item is not None and item.status == "active" else None

    async def _mark_superseded(
        self,
        index: list[MemoryItem],
        old_ids: list[str],
        *,
        new_item: MemoryItem,
        changed_items: list[MemoryItem],
    ) -> int:
        old_id_set = set(old_ids)
        count = 0
        now = self.now_fn()
        for item in index:
            if item.id not in old_id_set or item.status == "superseded":
                continue
            item.status = "superseded"
            item.updated_at = now
            changed_items.append(item)
            count += 1
            if self.memory2_store is not None:
                try:
                    await self.memory2_store.record_replacement(
                        item.id,
                        new_item.id,
                        "explicit" if item.id in new_item.supersedes else "procedure_key",
                    )
                    await self.memory2_store.update_item(
                        item.id,
                        {
                            "status": "superseded",
                            "updated_at": now.isoformat(),
                            "extra_json": {"superseded_by": new_item.id},
                        },
                    )
                except Exception:
                    continue
        return count

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

    async def _merge_candidate(
        self,
        item: MemoryItem,
        candidate: dict[str, object],
        *,
        tag: str,
        now: datetime,
    ) -> None:
        item.metadata["reinforcement"] = int(item.metadata.get("reinforcement") or 0) + 1
        item.importance = max(item.importance, _candidate_float(candidate, "importance", default=0.5))
        item.confidence = max(item.confidence, _candidate_float(candidate, "confidence", default=0.5))
        item.updated_at = now
        item.last_used_at = now
        item.tags = sorted(set(item.tags) | {tag})
        candidate_metadata = _candidate_metadata(candidate)
        candidate_tags = candidate_metadata.pop("tags", [])
        if isinstance(candidate_tags, list):
            item.tags = sorted(set(item.tags) | {str(tag) for tag in candidate_tags})
        item.metadata.update(candidate_metadata)

        source_ref = str(candidate.get("source_ref") or "").strip()
        source_refs = item.metadata.get("source_refs", [])
        if not isinstance(source_refs, list):
            source_refs = []
        merged_refs = {str(ref) for ref in source_refs if str(ref).strip()}
        if item.source_ref:
            merged_refs.add(item.source_ref)
        if source_ref:
            if not item.source_ref:
                item.source_ref = source_ref
            merged_refs.add(source_ref)
        item.metadata["source_refs"] = sorted(merged_refs)
        if self.memory2_store is not None:
            try:
                await self.memory2_store.reinforce_item(item.id)
                await self.memory2_store.update_item(
                    item.id,
                    {
                        "importance": item.importance,
                        "confidence": item.confidence,
                        "last_seen_at": now.isoformat(),
                        "extra_json": {
                            "tags": list(item.tags),
                            "source_refs": list(item.metadata.get("source_refs", [])),
                            "reinforcement": item.metadata["reinforcement"],
                            **{
                                key: value
                                for key, value in item.metadata.items()
                                if key not in {"tags", "source_refs", "reinforcement"}
                            },
                        },
                    },
                )
            except Exception:
                return


def _candidate_metadata(candidate: dict[str, object]) -> dict[str, Any]:
    metadata = candidate.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    return {}


def _candidate_with_decision(
    candidate: dict[str, object],
    decision: MergeDecision,
) -> dict[str, object]:
    updated = dict(candidate)
    metadata = _candidate_metadata(updated)
    metadata.update(dict(decision.metadata))
    if decision.new_body:
        metadata["body"] = decision.new_body
    if decision.tags:
        existing_tags = metadata.get("tags", [])
        if not isinstance(existing_tags, list):
            existing_tags = []
        metadata["tags"] = sorted({*map(str, existing_tags), *map(str, decision.tags)})
    if decision.reason:
        metadata["llm_merge_reason"] = decision.reason
    metadata["llm_merge_confidence"] = decision.confidence
    metadata["llm_action"] = decision.action
    updated["metadata"] = metadata
    if decision.new_summary:
        updated["content"] = decision.new_summary
    return updated


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


def _requires_review(candidate: dict[str, object], tag: str) -> bool:
    if _candidate_has_supersede_target(candidate):
        return False
    return (
        tag == "correction"
        or _as_bool(candidate.get("correction"))
        or _as_bool(candidate.get("requires_review"))
    )


def _item_to_vector_record(item: MemoryItem) -> VectorMemoryRecord:
    return VectorMemoryRecord(
        item_id=item.id,
        memory_type=item.type,
        summary=item.text,
        source_ref=item.source_ref or item.source,
        happened_at=item.happened_at or item.last_used_at or item.created_at,
        status=item.status,
        created_at=item.created_at,
        updated_at=item.updated_at,
        embedding=None,
        metadata={
            "tags": list(item.tags),
            "importance": item.importance,
            "confidence": item.confidence,
            "reinforcement": int(item.metadata.get("reinforcement") or 0),
            "source_refs": list(item.metadata.get("source_refs", []))
            if isinstance(item.metadata.get("source_refs"), list)
            else [],
            "source": item.source,
            "emotional_weight": item.emotional_weight,
        },
    )


def _item_to_similar_dict(item: MemoryItem, *, score: float) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type,
        "kind": item.type,
        "summary": item.text,
        "body": str(item.metadata.get("body") or item.text),
        "status": item.status,
        "score": float(score),
        "source_ref": item.source_ref,
        "metadata": {
            key: value
            for key, value in item.metadata.items()
            if key not in {"embedding", "embedding_json", "api_key", "token"}
        },
    }


def _text_similarity(left: str, right: str) -> float:
    left_terms = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", left.lower()))
    right_terms = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", right.lower()))
    if not left_terms or not right_terms:
        return 0.0
    overlap = len(left_terms & right_terms)
    return overlap / max(len(left_terms | right_terms), 1)


def _candidate_has_supersede_target(candidate: dict[str, object]) -> bool:
    metadata = _candidate_metadata(candidate)
    for key in ("supersedes", "supersedes_ids", "replaces", "replace_ids"):
        if _coerce_id_list(metadata.get(key)):
            return True
    return False


def _candidate_supersede_ids(
    candidate: dict[str, object],
    index: list[MemoryItem],
) -> list[str]:
    metadata = _candidate_metadata(candidate)
    existing_ids = {item.id for item in index}
    ids: list[str] = []
    for key in ("supersedes", "supersedes_ids", "replaces", "replace_ids"):
        ids.extend(_coerce_id_list(metadata.get(key)))
    return [memory_id for memory_id in _dedupe_ids(ids) if memory_id in existing_ids]


def _procedure_supersede_ids(
    candidate: dict[str, object],
    index: list[MemoryItem],
    *,
    tag: str,
    memory_type: str,
) -> list[str]:
    if tag != "procedure" and memory_type not in {"procedure", "instruction", "requested_memory"}:
        return []
    candidate_key = _candidate_procedure_key(candidate)
    if not candidate_key:
        return []
    ids: list[str] = []
    for item in index:
        if item.status != "active":
            continue
        if item.type not in {"procedure", "instruction", "requested_memory"}:
            continue
        if _metadata_procedure_key(item.metadata) == candidate_key:
            ids.append(item.id)
    return ids


def _candidate_procedure_key(candidate: dict[str, object]) -> str:
    return _metadata_procedure_key(_candidate_metadata(candidate))


def _metadata_procedure_key(metadata: dict[str, Any]) -> str:
    for key in ("procedure_key", "tool_requirement"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value.lower()
    return ""


def _coerce_id_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,\s]+", text) if part.strip()]


def _dedupe_ids(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in ids:
        memory_id = str(raw).strip()
        if memory_id and memory_id not in seen:
            seen.add(memory_id)
            deduped.append(memory_id)
    return deduped


def _memory2_content_hash(text: str) -> str:
    normalized = _normalize(f"{text}\n{text}")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _truncate(text: str, limit: int) -> str:
    clean = str(text or "").replace("\n", " ").strip()
    return clean if len(clean) <= limit else clean[:limit] + "..."
