from __future__ import annotations

from datetime import datetime, timezone
import re

from memory.models import MemoryItem


class MemorySearch:
    def search(self, items: list[MemoryItem], query: str, limit: int = 8) -> list[MemoryItem]:
        terms = _terms(query)
        scored: list[tuple[float, MemoryItem]] = []
        for item in items:
            if item.status != "active":
                continue
            score = self.score(item, terms)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def score(self, item: MemoryItem, terms: set[str]) -> float:
        text_terms = _terms(item.text)
        tag_terms = {tag.lower() for tag in item.tags}
        keyword_score = len(terms & text_terms) / max(len(terms), 1)
        tag_score = len(terms & tag_terms) / max(len(terms), 1)
        recency_score = _recency_score(item.updated_at)
        type_bonus = 0.1 if item.type in {"goal", "project", "preference", "instruction"} else 0.0
        return (
            keyword_score * 0.45
            + tag_score * 0.2
            + item.importance * 0.2
            + recency_score * 0.1
            + item.confidence * 0.05
            + type_bonus
        )


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", lowered))
    terms.update(char for char in lowered if "\u4e00" <= char <= "\u9fff")
    return terms


def _recency_score(updated_at: datetime) -> float:
    now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age_days = max((now - updated_at).total_seconds() / 86400, 0)
    if age_days < 7:
        return 1.0
    if age_days < 30:
        return 0.7
    if age_days < 180:
        return 0.4
    return 0.1
