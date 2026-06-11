from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import json

from agent.llm import LLMProvider
from bootstrap.config import MemoryConfig


ALLOWED_MERGE_ACTIONS = {
    "add",
    "merge",
    "reinforce",
    "supersede",
    "requires_review",
    "skip",
}


@dataclass(slots=True)
class MergeDecision:
    action: str = "requires_review"
    target_ids: list[str] = field(default_factory=list)
    new_summary: str = ""
    new_body: str = ""
    confidence: float = 0.0
    reason: str = ""
    sensitive: bool = False
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MergeCandidate:
    pending_id: str = ""
    source_ref: str = ""
    tag: str = ""
    content: str = ""
    confidence: float = 0.0
    importance: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    similar_active: list[dict[str, Any]] = field(default_factory=list)
    explicit_supersedes: list[str] = field(default_factory=list)


class LLMMemoryMergeResolver:
    def __init__(
        self,
        *,
        config: MemoryConfig,
        provider: LLMProvider | None,
    ) -> None:
        self.config = config
        self.provider = provider

    async def resolve(self, candidate: MergeCandidate) -> MergeDecision | None:
        if not bool(getattr(self.config, "llm_merge_enabled", False)):
            return None
        if self.provider is None or getattr(self.provider, "name", "") == "echo":
            return None

        try:
            response = await self.provider.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _candidate_prompt(candidate, self.config)},
                ],
                temperature=0.0,
                timeout_seconds=20.0,
                purpose="memory_merge",
            )
        except Exception as exc:
            return MergeDecision(
                action="requires_review",
                confidence=0.0,
                reason=f"LLM merge failed: {exc}",
                metadata={"error": str(exc), "fallback": "provider_error"},
            )

        try:
            payload = _loads_json(response)
        except ValueError as exc:
            return MergeDecision(
                action="requires_review",
                confidence=0.0,
                reason="LLM merge returned invalid JSON",
                metadata={"error": str(exc), "raw_response": _truncate(response, 500)},
            )

        decision = _decision_from_payload(payload)
        return self._validate_decision(candidate, decision)

    def _validate_decision(
        self,
        candidate: MergeCandidate,
        decision: MergeDecision,
    ) -> MergeDecision:
        if decision.action not in ALLOWED_MERGE_ACTIONS:
            decision.metadata["suggested_action"] = decision.action
            decision.action = "requires_review"
            decision.reason = decision.reason or "unsupported LLM action"

        valid_targets = {
            str(item.get("id") or "")
            for item in candidate.similar_active
            if str(item.get("id") or "").strip()
        } | set(candidate.explicit_supersedes)
        original_targets = list(decision.target_ids)
        decision.target_ids = [
            target_id for target_id in _dedupe_ids(decision.target_ids) if target_id in valid_targets
        ]
        if original_targets and not decision.target_ids:
            decision.metadata["invalid_target_ids"] = original_targets
            decision.action = "requires_review"
            decision.reason = decision.reason or "LLM returned unknown target id"

        if decision.action in {"merge", "reinforce", "supersede"} and not decision.target_ids:
            decision.metadata["suggested_action"] = decision.action
            decision.action = "requires_review"
            decision.reason = decision.reason or "target_ids are required for this action"

        if _looks_sensitive(candidate, decision):
            decision.sensitive = True

        threshold = float(getattr(self.config, "llm_merge_confidence_threshold", 0.75))
        if decision.confidence < threshold:
            suggested_action = decision.action
            decision.action = "requires_review"
            decision.metadata["suggested_action"] = suggested_action
            decision.reason = decision.reason or "LLM confidence below threshold"

        if (
            decision.sensitive
            and bool(getattr(self.config, "llm_merge_require_review_for_sensitive", True))
        ):
            suggested_action = decision.action
            decision.action = "requires_review"
            decision.metadata["suggested_action"] = suggested_action
            decision.reason = decision.reason or "sensitive memory requires review"

        if (
            decision.action == "supersede"
            and not bool(getattr(self.config, "llm_merge_allow_auto_supersede", False))
            and not candidate.explicit_supersedes
        ):
            decision.action = "requires_review"
            decision.metadata["suggested_action"] = "supersede"
            decision.reason = decision.reason or "auto supersede is disabled"

        if decision.action == "skip" and not decision.reason.strip():
            decision.action = "requires_review"
            decision.metadata["suggested_action"] = "skip"
            decision.reason = "skip requires a clear reason"

        return decision


class RuleBasedMergeResolver:
    async def resolve(self, candidate: MergeCandidate) -> MergeDecision | None:
        if candidate.explicit_supersedes:
            return MergeDecision(
                action="supersede",
                target_ids=list(candidate.explicit_supersedes),
                new_summary=candidate.content,
                confidence=1.0,
                reason="explicit supersedes metadata",
            )
        return None


_SYSTEM_PROMPT = """You resolve long-term memory merge decisions.
Use only explicit information from the pending candidate and the listed active memories.
Do not turn assistant suggestions, guesses, or inferences into user facts.
Temporary tasks should not become long-term memories.
If a correction has no clear replacement target, choose requires_review.
Mark health, legal, financial, safety, or similarly high-risk information as sensitive=true.
When uncertain, choose requires_review.
Return JSON only, with no markdown or prose."""


def _candidate_prompt(candidate: MergeCandidate, config: MemoryConfig) -> str:
    max_candidates = int(getattr(config, "llm_merge_max_candidates", 5))
    similar = [
        {
            "id": str(item.get("id") or ""),
            "type": str(item.get("type") or item.get("kind") or ""),
            "summary": str(item.get("summary") or ""),
            "body": _truncate(str(item.get("body") or item.get("text") or ""), 600),
            "status": str(item.get("status") or ""),
            "score": item.get("score"),
            "source_ref": str(item.get("source_ref") or ""),
            "metadata": _trim_metadata(item.get("metadata")),
        }
        for item in candidate.similar_active[:max_candidates]
    ]
    payload = {
        "pending_candidate": {
            "pending_id": candidate.pending_id,
            "source_ref": candidate.source_ref,
            "tag": candidate.tag,
            "content": candidate.content,
            "confidence": candidate.confidence,
            "importance": candidate.importance,
            "metadata": _trim_metadata(candidate.metadata),
            "explicit_supersedes": candidate.explicit_supersedes,
        },
        "similar_active": similar,
        "allowed_actions": sorted(ALLOWED_MERGE_ACTIONS),
        "output_schema": {
            "action": "add|merge|reinforce|supersede|requires_review|skip",
            "target_ids": ["..."],
            "new_summary": "...",
            "new_body": "...",
            "confidence": 0.0,
            "reason": "...",
            "sensitive": False,
            "tags": ["..."],
            "metadata": {},
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _decision_from_payload(payload: dict[str, Any]) -> MergeDecision:
    action = str(payload.get("action") or "requires_review").strip().lower()
    return MergeDecision(
        action=action,
        target_ids=_coerce_str_list(payload.get("target_ids")),
        new_summary=str(payload.get("new_summary") or "").strip(),
        new_body=str(payload.get("new_body") or "").strip(),
        confidence=_coerce_float(payload.get("confidence"), default=0.0),
        reason=str(payload.get("reason") or "").strip(),
        sensitive=_coerce_bool(payload.get("sensitive")),
        tags=_coerce_str_list(payload.get("tags")),
        metadata=dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {},
    )


def _loads_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json  # type: ignore
        except Exception as exc:
            raise ValueError("invalid JSON and json_repair unavailable") from exc
        parsed = json.loads(repair_json(raw))
    if not isinstance(parsed, dict):
        raise ValueError("LLM merge response must be a JSON object")
    return parsed


def _looks_sensitive(candidate: MergeCandidate, decision: MergeDecision) -> bool:
    text = " ".join(
        [
            candidate.tag,
            candidate.content,
            decision.new_summary,
            decision.new_body,
            json.dumps(candidate.metadata, ensure_ascii=False),
        ]
    ).lower()
    sensitive_terms = {
        "health",
        "medical",
        "medicine",
        "diagnosis",
        "therapy",
        "安全",
        "医疗",
        "健康",
        "诊断",
        "法律",
        "legal",
        "finance",
        "financial",
        "bank",
        "investment",
        "safety",
        "password",
    }
    return any(term in text for term in sensitive_terms)


def _trim_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    trimmed: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"embedding", "embedding_json", "api_key", "token"}:
            continue
        text = str(item)
        trimmed[str(key)] = item if len(text) <= 500 else text[:500] + "..."
    return trimmed


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _dedupe_ids(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in ids:
        item_id = str(raw).strip()
        if item_id and item_id not in seen:
            seen.add(item_id)
            deduped.append(item_id)
    return deduped


def _truncate(text: str, limit: int) -> str:
    clean = str(text or "")
    return clean if len(clean) <= limit else clean[:limit] + "..."
