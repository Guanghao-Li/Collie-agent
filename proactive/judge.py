from __future__ import annotations

import json
import logging

from agent.llm import LLMProvider
from memory.runtime import MemoryRuntime
from proactive.models import ProactiveCandidate, ProactiveDecision, ProactivePrefilterDecision


class ProactiveJudge:
    def __init__(
        self,
        llm_provider: LLMProvider,
        memory_runtime: MemoryRuntime,
        fast_llm_provider: LLMProvider | None = None,
    ) -> None:
        self.llm_provider = llm_provider
        self.main_llm_provider = llm_provider
        self.fast_llm_provider = fast_llm_provider or llm_provider
        self.memory_runtime = memory_runtime
        self.main_judge_calls = 0
        self._logger = logging.getLogger(__name__)

    async def fast_prefilter(
        self,
        candidate: ProactiveCandidate,
        min_score: float,
    ) -> ProactivePrefilterDecision:
        profile = await self.memory_runtime.read_profile()
        recent = await self.memory_runtime.read_recent_context()
        if (
            self.fast_llm_provider is self.main_llm_provider
            or getattr(self.fast_llm_provider, "name", "") == "echo"
        ):
            score = _heuristic_score(candidate, profile, recent)
            return ProactivePrefilterDecision(
                relevant=score >= min_score,
                rough_score=score,
                reason="heuristic_prefilter",
            )

        prompt = (
            "请判断这条主动推送候选是否与用户相关。只返回 JSON。\n"
            "JSON 字段：relevant(bool), rough_score(float), reason(str)。\n\n"
            f"用户画像：{profile}\n"
            f"近期上下文：{recent}\n"
            f"候选标题：{candidate.title}\n"
            f"候选内容：{candidate.content}"
        )
        try:
            response = await self.fast_llm_provider.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                timeout_seconds=10,
                purpose="proactive_fast_prefilter",
            )
            data = json.loads(response)
            score = float(data.get("rough_score", 0.0))
            return ProactivePrefilterDecision(
                relevant=bool(data.get("relevant", score >= min_score)),
                rough_score=score,
                reason=str(data.get("reason", "fast_prefilter")),
            )
        except Exception:
            self._logger.warning("proactive fast prefilter 失败，使用启发式规则。", exc_info=True)
            score = _heuristic_score(candidate, profile, recent)
            return ProactivePrefilterDecision(
                relevant=score >= min_score,
                rough_score=score,
                reason="fallback_heuristic_prefilter",
            )

    async def judge(
        self,
        candidate: ProactiveCandidate,
        min_score: float,
        push_history: list[str],
    ) -> ProactiveDecision:
        self.main_judge_calls += 1
        profile = await self.memory_runtime.read_profile()
        recent = await self.memory_runtime.read_recent_context()
        score = _heuristic_score(candidate, profile, recent)
        should_push = score >= min_score and candidate.id not in push_history
        reason = "与用户画像或近期上下文相关" if should_push else "低于阈值或已经推送过"
        message = f"{candidate.title}\n\n{candidate.content}".strip()
        return ProactiveDecision(candidate, should_push, score, reason, message)


def _heuristic_score(candidate: ProactiveCandidate, profile: str, recent: str) -> float:
    text = f"{candidate.title} {candidate.content}".lower()
    context = f"{profile} {recent}".lower()
    score = 0.45
    if any(word in text for word in ["goal", "project", "follow", "reminder", "deadline", "目标", "项目", "跟进", "提醒", "截止"]):
        score += 0.25
    terms = {term for term in text.split() if len(term) > 4}
    context_terms = {term for term in context.split() if len(term) > 4}
    if terms & context_terms:
        score += 0.2
    if candidate.source in {"manual", "memory_reminder"}:
        score += 0.15
    return min(score, 1.0)
