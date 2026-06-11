from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Iterable

from bootstrap.config import MemoryConfig
from memory.engine import EvidenceRef, MemoryQuery, MemoryQueryResult, MemoryRecord
from memory.markdown_store import MarkdownMemoryStore
from memory.memory2_store import SQLiteMemory2Store, memory_item_from_row
from memory.models import MemoryItem
from memory.search import MemorySearch
from memory.store import MemoryStore
from memory.vector_store import VectorMemoryStore


PROCEDURE_TYPES = {"procedure", "instruction", "requested_memory"}
TIMELINE_TYPES = {"event", "summary", "reflection"}
PROFILE_TYPES = {"identity", "relationship", "habit", "health_long_term"}


@dataclass(slots=True)
class Candidate:
    id: str
    kind: str
    summary: str
    item: MemoryItem | None = None
    score: float = 0.0
    engine_kind: str = "hybrid"
    source_ref: str = ""
    happened_at: datetime | None = None
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)


class MemoryRetriever:
    def __init__(
        self,
        *,
        store: MemoryStore,
        markdown_store: MarkdownMemoryStore,
        memory2_store: SQLiteMemory2Store | None,
        vector_store: VectorMemoryStore,
        search: MemorySearch,
        config: MemoryConfig,
    ) -> None:
        self.store = store
        self.markdown_store = markdown_store
        self.memory2_store = memory2_store
        self.vector_store = vector_store
        self.search = search
        self.config = config

    async def retrieve(self, query: MemoryQuery) -> MemoryQueryResult:
        text = (query.text or "").strip()
        kind = query.effective_kind()
        metadata: dict[str, Any] = {
            "intent": query.intent or kind,
            "retrieval_mode": (
                "hybrid" if self.vector_store.is_enabled() else "keyword_only"
            ),
            "vector_enabled": self.vector_store.is_enabled(),
        }
        allowed_kinds = set(query.filters.kinds)
        active_items = [
            item
            for item in self.store.read_index()
            if item.status == "active" and (not allowed_kinds or item.type in allowed_kinds)
        ]

        lanes: list[list[Candidate]] = []
        vector_lane = await self._vector_lane(query)
        if vector_lane.error:
            metadata["vector_error"] = vector_lane.error
        if vector_lane.candidates:
            lanes.append(vector_lane.candidates)

        keyword_lane = self._keyword_lane(query, active_items)
        if keyword_lane:
            lanes.append(keyword_lane)

        if self._should_use_procedure_lane(query):
            procedure_lane = self._procedure_lane(query, active_items)
            if procedure_lane:
                lanes.append(procedure_lane)

        if self._should_use_timeline_lane(query):
            timeline_lane = await self._timeline_lane(query, active_items)
            if timeline_lane:
                lanes.append(timeline_lane)

        candidates = rrf_merge(
            lanes,
            rrf_k=int(getattr(self.config, "hybrid_rrf_k", 60)),
            procedure_boost=float(getattr(self.config, "procedure_boost", 0.15)),
            reinforcement_boost=float(getattr(self.config, "reinforcement_boost", 0.05)),
        )
        candidates = candidates[: max(int(query.limit), 0)]
        records = [self._candidate_to_record(candidate, query) for candidate in candidates]
        items = [candidate.item for candidate in candidates if candidate.item is not None]
        content = "\n".join(f"- [{record.kind}] {record.summary}" for record in records)

        await self._record_accesses(records, text)
        self._touch_items([item.id for item in items])

        metadata.update(
            {
                "count": len(records),
                "search_backend": _search_backend_name(
                    vector_enabled=self.vector_store.is_enabled(),
                    vector_count=len(vector_lane.candidates),
                    keyword_count=len(keyword_lane),
                ),
                "lanes": _lane_names(candidates),
                "vector_candidates": len(vector_lane.candidates),
                "keyword_candidates": len(keyword_lane),
            }
        )
        if not self.vector_store.is_enabled():
            metadata["vector_disabled"] = True

        return MemoryQueryResult(
            items=items,
            content=content,
            text_block=content,
            metadata=metadata,
            records=records,
            raw={
                "records": [
                    {
                        "id": record.id,
                        "kind": record.kind,
                        "score": record.score,
                        "signals": dict(record.signals),
                    }
                    for record in records
                ],
            },
        )

    async def build_injection(self, query: MemoryQuery) -> MemoryQueryResult:
        retrieval_query = MemoryQuery(
            kind="search",
            text=query.text,
            limit=max(query.limit, int(getattr(self.config, "hybrid_keyword_top_k", 12))),
            recent_messages=query.recent_messages,
            intent=query.intent or "context",
            scope=query.scope,
            filters=query.filters,
            context=query.context,
            timestamp=query.timestamp,
        )
        result = await self.retrieve(retrieval_query)
        records = [
            record
            for record in result.records
            if str(record.signals.get("status") or "active") == "active"
        ]
        text_block, injected_ids = self._render_injection(records)
        for record in records:
            record.injected = record.id in injected_ids
        result.records = records
        result.items = [item for item in result.items if item.id in {record.id for record in records}]
        result.content = text_block
        result.text_block = text_block
        result.metadata.update(
            {
                "injection_budget_chars": int(
                    getattr(self.config, "memory_injection_budget_chars", 3500)
                ),
                "injected_count": len(injected_ids),
                "injected_ids": injected_ids,
            }
        )
        return result

    async def _vector_lane(self, query: MemoryQuery) -> "_LaneResult":
        text = (query.text or "").strip()
        if (
            not text
            or not getattr(self.config, "enable_vector_memory", False)
            or not self.vector_store.is_enabled()
        ):
            return _LaneResult([])
        try:
            matches = await self.vector_store.search(
                text,
                top_k=int(getattr(self.config, "vector_top_k", 12)),
                score_threshold=float(getattr(self.config, "vector_score_threshold", 0.72)),
            )
        except Exception as exc:
            return _LaneResult([], error=str(exc))

        items_by_id = {item.id: item for item in self.store.read_index() if item.status == "active"}
        candidates: list[Candidate] = []
        for match in matches:
            record = match.record
            if record.status != "active":
                continue
            item = items_by_id.get(record.item_id)
            allowed_kinds = set(query.filters.kinds)
            candidate_kind = item.type if item else record.memory_type
            if allowed_kinds and candidate_kind not in allowed_kinds:
                continue
            metadata = dict(record.metadata)
            candidate = Candidate(
                id=record.item_id,
                kind=candidate_kind,
                summary=item.text if item else record.summary,
                item=item,
                score=float(match.score),
                engine_kind="memory2" if item is None else "hybrid",
                source_ref=(item.source_ref if item else record.source_ref) or "",
                happened_at=(item.happened_at if item else record.happened_at),
                status=item.status if item else record.status,
                metadata=metadata,
                signals={
                    "vector_score": float(match.score),
                    "lane_sources": ["vector"],
                    "source_ref": (item.source_ref if item else record.source_ref) or "",
                    "status": item.status if item else record.status,
                    "importance": _candidate_importance(item, metadata),
                    "confidence": _candidate_confidence(item, metadata),
                    "reinforcement": _candidate_reinforcement(item, metadata),
                },
            )
            candidates.append(candidate)
        return _LaneResult(candidates)

    def _keyword_lane(
        self,
        query: MemoryQuery,
        active_items: list[MemoryItem],
    ) -> list[Candidate]:
        text = (query.text or "").strip()
        limit = int(getattr(self.config, "hybrid_keyword_top_k", 12))
        terms = _terms(text)
        if text:
            results = self.search.search(active_items, text, limit)
        elif query.intent in {"context", "interest"}:
            results = sorted(
                active_items,
                key=lambda item: (
                    -float(item.importance),
                    -float(item.confidence),
                    item.type,
                    item.text.lower(),
                ),
            )[:limit]
        else:
            results = []
        candidates: list[Candidate] = []
        for item in results:
            keyword_score = self.search.score(item, terms) if text else item.importance
            candidates.append(
                self._item_candidate(
                    item,
                    lane="keyword",
                    score=keyword_score,
                    signals={"keyword_score": keyword_score},
                )
            )
        return candidates

    def _procedure_lane(
        self,
        query: MemoryQuery,
        active_items: list[MemoryItem],
    ) -> list[Candidate]:
        text = (query.text or "").strip()
        procedure_items = [
            item
            for item in active_items
            if item.type in PROCEDURE_TYPES or any(tag in PROCEDURE_TYPES for tag in item.tags)
        ]
        limit = int(getattr(self.config, "hybrid_keyword_top_k", 12))
        terms = _terms(text)
        if text:
            scored = [
                (self.search.score(item, terms), item)
                for item in procedure_items
            ]
            scored = [(score, item) for score, item in scored if score > 0]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            selected = scored[:limit]
        else:
            selected = [
                (item.importance + item.confidence * 0.25, item)
                for item in sorted(
                    procedure_items,
                    key=lambda item: (-item.importance, -item.confidence, item.text.lower()),
                )[:limit]
            ]
        return [
            self._item_candidate(
                item,
                lane="procedure",
                score=score,
                signals={"procedure_score": score},
            )
            for score, item in selected
        ]

    async def _timeline_lane(
        self,
        query: MemoryQuery,
        active_items: list[MemoryItem],
    ) -> list[Candidate]:
        text = (query.text or "").strip().lower()
        candidates: list[Candidate] = []
        event_items = [item for item in active_items if item.type in TIMELINE_TYPES]
        for item in event_items:
            if text and text not in item.text.lower() and not _terms(text) & _terms(item.text):
                continue
            if not _within_time_filter(item.happened_at or item.created_at, query):
                continue
            score = item.importance + item.confidence * 0.25 + _recency_bonus(item)
            candidates.append(
                self._item_candidate(
                    item,
                    lane="timeline",
                    score=score,
                    signals={"timeline_score": score},
                )
            )

        if self.memory2_store is not None:
            for row in await self.memory2_store.find_active_by_type(
                "event",
                limit=int(getattr(self.config, "hybrid_keyword_top_k", 12)),
            ):
                memory_id = str(row.get("id") or "")
                if any(candidate.id == memory_id for candidate in candidates):
                    continue
                item = memory_item_from_row(row)
                if text and text not in item.text.lower() and not _terms(text) & _terms(item.text):
                    continue
                if not _within_time_filter(item.happened_at or item.created_at, query):
                    continue
                score = float(row.get("importance") or 0.5) + _recency_bonus(item)
                candidates.append(
                    self._item_candidate(
                        item,
                        lane="timeline",
                        score=score,
                        engine_kind="memory2",
                        signals={"timeline_score": score},
                    )
                )

        history_lines = self._history_candidates(query)
        candidates.extend(history_lines)
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return candidates[: int(getattr(self.config, "hybrid_keyword_top_k", 12))]

    def _history_candidates(self, query: MemoryQuery) -> list[Candidate]:
        text = (query.text or "").strip().lower()
        lines = [
            line.strip()
            for line in self.markdown_store.read_text(self.markdown_store.history_md).splitlines()
            if line.strip().startswith("[")
        ]
        candidates: list[Candidate] = []
        for index, line in enumerate(lines):
            happened_at = _parse_history_timestamp(line)
            if text and text not in line.lower() and not _terms(text) & _terms(line):
                continue
            if not _within_time_filter(happened_at, query):
                continue
            candidates.append(
                Candidate(
                    id=f"history:{index}",
                    kind="event",
                    summary=line,
                    score=0.6 + _datetime_recency_bonus(happened_at),
                    engine_kind="markdown",
                    happened_at=happened_at,
                    status="active",
                    signals={
                        "timeline_score": 0.6,
                        "lane_sources": ["timeline"],
                        "status": "active",
                        "source_ref": "HISTORY.md",
                        "importance": 0.5,
                        "confidence": 0.5,
                        "reinforcement": 0,
                    },
                )
            )
        return candidates[-int(getattr(self.config, "hybrid_keyword_top_k", 12)) :]

    def _item_candidate(
        self,
        item: MemoryItem,
        *,
        lane: str,
        score: float,
        signals: dict[str, Any],
        engine_kind: str = "legacy_index",
    ) -> Candidate:
        metadata = dict(item.metadata)
        metadata.setdefault("tags", list(item.tags))
        metadata.setdefault("importance", item.importance)
        metadata.setdefault("confidence", item.confidence)
        metadata.setdefault("reinforcement", int(metadata.get("reinforcement") or 0))
        source_ref = item.source_ref or item.source
        return Candidate(
            id=item.id,
            kind=item.type,
            summary=item.text,
            item=item,
            score=float(score),
            engine_kind=engine_kind,
            source_ref=source_ref,
            happened_at=item.happened_at,
            status=item.status,
            metadata=metadata,
            signals={
                **signals,
                "lane_sources": [lane],
                "source_ref": source_ref,
                "happened_at": _format_datetime(item.happened_at),
                "status": item.status,
                "importance": item.importance,
                "confidence": item.confidence,
                "reinforcement": int(metadata.get("reinforcement") or 0),
            },
        )

    def _candidate_to_record(self, candidate: Candidate, query: MemoryQuery) -> MemoryRecord:
        source_ref = candidate.source_ref or str(candidate.signals.get("source_ref") or "")
        evidence = [
            EvidenceRef(
                kind="memory",  # type: ignore[arg-type]
                refs=[candidate.id],
                source_ref=source_ref,
                metadata={"engine_kind": candidate.engine_kind},
            )
        ]
        signals = {
            "vector_score": 0.0,
            "keyword_score": 0.0,
            "procedure_score": 0.0,
            "timeline_score": 0.0,
            "rrf_score": 0.0,
            "importance": _candidate_importance(candidate.item, candidate.metadata),
            "confidence": _candidate_confidence(candidate.item, candidate.metadata),
            "reinforcement": _candidate_reinforcement(candidate.item, candidate.metadata),
            "source_ref": source_ref,
            "happened_at": _format_datetime(candidate.happened_at),
            "lane_sources": [],
            "status": candidate.status,
            "intent": query.intent or query.effective_kind(),
            **candidate.signals,
        }
        return MemoryRecord(
            id=candidate.id,
            kind=candidate.kind,
            summary=candidate.summary,
            score=candidate.score,
            engine_kind=candidate.engine_kind,
            evidence=evidence,
            signals=signals,
            injected=False,
        )

    def _render_injection(self, records: list[MemoryRecord]) -> tuple[str, list[str]]:
        budget = max(int(getattr(self.config, "memory_injection_budget_chars", 3500)), 0)
        if budget <= 0:
            return "", []
        ordered = sorted(records, key=_injection_sort_key)
        grouped: dict[str, list[MemoryRecord]] = {
            "Procedures": [],
            "Preferences": [],
            "Profile": [],
            "Projects": [],
            "Recent / Timeline": [],
            "Other": [],
        }
        for record in ordered:
            grouped[_injection_group(record.kind)].append(record)

        block = "Relevant memory:\n"
        if len(block) > budget:
            return block[:budget], []
        injected_ids: list[str] = []
        for group_name in grouped:
            group_records = sorted(
                grouped[group_name],
                key=lambda record: record.score,
                reverse=True,
            )
            if not group_records:
                continue
            group_header = f"\n## {group_name}\n"
            group_started = False
            for record in group_records:
                line = f"- {record.summary.strip()}\n"
                prefix = group_header if not group_started else ""
                addition = prefix + line
                if len(block) + len(addition) <= budget:
                    block += addition
                    group_started = True
                    injected_ids.append(record.id)
                    continue
                remaining = budget - len(block) - len(prefix)
                if (
                    (group_name == "Procedures" or not injected_ids)
                    and record.id not in injected_ids
                    and remaining > 10
                ):
                    truncated = line[: max(0, remaining - 4)].rstrip() + "...\n"
                    block += prefix + truncated[:remaining]
                    injected_ids.append(record.id)
                    group_started = True
                break
        return block[:budget], injected_ids

    async def _record_accesses(self, records: list[MemoryRecord], query_text: str) -> None:
        if self.memory2_store is None:
            return
        for record in records:
            if record.id.startswith("history:"):
                continue
            try:
                await self.memory2_store.record_access(record.id, query_text, record.score)
            except Exception:
                continue

    def _touch_items(self, item_ids: Iterable[str]) -> None:
        ids = {item_id for item_id in item_ids if item_id and not item_id.startswith("history:")}
        if not ids:
            return
        items = self.store.read_index()
        now = datetime.now(timezone.utc)
        changed = False
        for item in items:
            if item.id in ids:
                item.last_used_at = now
                changed = True
        if changed:
            self.store.write_index(items)

    def _should_use_procedure_lane(self, query: MemoryQuery) -> bool:
        return (
            query.intent in {"procedure", "context"}
            or query.effective_kind() == "procedure"
            or query.effective_kind() == "context"
        )

    def _should_use_timeline_lane(self, query: MemoryQuery) -> bool:
        return query.intent == "timeline" or query.effective_kind() == "timeline"


@dataclass(slots=True)
class _LaneResult:
    candidates: list[Candidate]
    error: str = ""


def rrf_merge(
    lanes: list[list[Candidate]],
    *,
    rrf_k: int = 60,
    procedure_boost: float = 0.15,
    reinforcement_boost: float = 0.05,
) -> list[Candidate]:
    by_id: dict[str, Candidate] = {}
    for lane in lanes:
        for rank, candidate in enumerate(lane, start=1):
            existing = by_id.get(candidate.id)
            if existing is None:
                existing = Candidate(
                    id=candidate.id,
                    kind=candidate.kind,
                    summary=candidate.summary,
                    item=candidate.item,
                    engine_kind=candidate.engine_kind,
                    source_ref=candidate.source_ref,
                    happened_at=candidate.happened_at,
                    status=candidate.status,
                    metadata=dict(candidate.metadata),
                    signals={
                        "lane_sources": [],
                        "rrf_score": 0.0,
                    },
                )
                by_id[candidate.id] = existing
            existing.signals["rrf_score"] = float(existing.signals.get("rrf_score") or 0.0) + (
                1.0 / (max(rrf_k, 1) + rank)
            )
            for key, value in candidate.signals.items():
                if key == "lane_sources":
                    sources = existing.signals.setdefault("lane_sources", [])
                    if not isinstance(sources, list):
                        sources = []
                        existing.signals["lane_sources"] = sources
                    for source in value if isinstance(value, list) else [value]:
                        if source not in sources:
                            sources.append(source)
                    continue
                if key.endswith("_score"):
                    existing.signals[key] = max(
                        float(existing.signals.get(key) or 0.0),
                        float(value or 0.0),
                    )
                    continue
                if key not in existing.signals or existing.signals[key] in {"", None, 0}:
                    existing.signals[key] = value
            if candidate.item is not None:
                existing.item = candidate.item
            if candidate.engine_kind == "memory2":
                existing.engine_kind = "memory2"
            elif existing.engine_kind not in {"memory2", "hybrid"}:
                existing.engine_kind = candidate.engine_kind

    for candidate in by_id.values():
        signals = candidate.signals
        vector_score = float(signals.get("vector_score") or 0.0)
        keyword_score = float(signals.get("keyword_score") or 0.0)
        procedure_score = float(signals.get("procedure_score") or 0.0)
        timeline_score = float(signals.get("timeline_score") or 0.0)
        importance = float(signals.get("importance") or 0.0)
        confidence = float(signals.get("confidence") or 0.0)
        reinforcement = float(signals.get("reinforcement") or 0.0)
        rrf_score = float(signals.get("rrf_score") or 0.0)
        recency = _datetime_recency_bonus(candidate.happened_at)
        final_score = (
            rrf_score
            + min(max(vector_score, 0.0), 1.0) * 0.35
            + min(max(keyword_score, 0.0), 1.0) * 0.25
            + min(max(timeline_score, 0.0), 1.0) * 0.15
            + importance * 0.05
            + confidence * 0.05
            + reinforcement * reinforcement_boost
            + (procedure_boost if candidate.kind in PROCEDURE_TYPES or procedure_score else 0.0)
            + recency
        )
        candidate.score = final_score
        signals["final_score"] = final_score
        signals["recency_bonus"] = recency
    return sorted(by_id.values(), key=lambda candidate: candidate.score, reverse=True)


def _lane_names(candidates: list[Candidate]) -> list[str]:
    names: list[str] = []
    for candidate in candidates:
        sources = candidate.signals.get("lane_sources", [])
        for source in sources if isinstance(sources, list) else [sources]:
            if source and source not in names:
                names.append(str(source))
    return names


def _search_backend_name(
    *,
    vector_enabled: bool,
    vector_count: int,
    keyword_count: int,
) -> str:
    if vector_enabled and vector_count:
        return "vector"
    if keyword_count:
        return "keyword"
    if vector_enabled:
        return "vector"
    return "keyword"


def _candidate_importance(item: MemoryItem | None, metadata: dict[str, Any]) -> float:
    if item is not None:
        return float(item.importance)
    return _coerce_float(metadata.get("importance"), default=0.5)


def _candidate_confidence(item: MemoryItem | None, metadata: dict[str, Any]) -> float:
    if item is not None:
        return float(item.confidence)
    return _coerce_float(metadata.get("confidence"), default=0.5)


def _candidate_reinforcement(item: MemoryItem | None, metadata: dict[str, Any]) -> int:
    if item is not None:
        return _coerce_int(item.metadata.get("reinforcement"), default=0)
    return _coerce_int(metadata.get("reinforcement"), default=0)


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", lowered))
    terms.update(char for char in lowered if "\u4e00" <= char <= "\u9fff")
    return terms


def _within_time_filter(value: datetime | None, query: MemoryQuery) -> bool:
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    start = query.filters.time_start
    end = query.filters.time_end
    if start is not None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if value < start:
            return False
    if end is not None:
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if value > end:
            return False
    return True


def _recency_bonus(item: MemoryItem) -> float:
    return _datetime_recency_bonus(item.happened_at or item.updated_at)


def _datetime_recency_bonus(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    age_days = max((datetime.now(timezone.utc) - value).total_seconds() / 86400, 0)
    if age_days < 7:
        return 0.05
    if age_days < 30:
        return 0.03
    if age_days < 180:
        return 0.01
    return 0.0


def _parse_history_timestamp(line: str) -> datetime | None:
    match = re.match(r"\[([^\]]+)\]", line.strip())
    if not match:
        return None
    raw = match.group(1).strip()
    for candidate in (raw, raw.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def _injection_group(kind: str) -> str:
    if kind in PROCEDURE_TYPES:
        return "Procedures"
    if kind == "preference":
        return "Preferences"
    if kind in PROFILE_TYPES:
        return "Profile"
    if kind in {"project", "goal"}:
        return "Projects"
    if kind in TIMELINE_TYPES:
        return "Recent / Timeline"
    return "Other"


def _injection_sort_key(record: MemoryRecord) -> tuple[int, float]:
    group_priority = {
        "Procedures": 0,
        "Preferences": 1,
        "Profile": 1,
        "Recent / Timeline": 2,
        "Projects": 2,
        "Other": 3,
    }
    return (group_priority[_injection_group(record.kind)], -record.score)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
