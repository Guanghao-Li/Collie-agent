from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import json
import re

from agent.llm import LLMProvider


SUPPORTED_INTENTS = {
    "general_chat",
    "tool_execution",
    "memory_add",
    "memory_correction",
    "memory_delete",
    "proactive_config",
    "drift_task_create",
    "dashboard_command",
    "plugin_management",
}

ROUTES = {
    "general_chat": "chat",
    "tool_execution": "tools",
    "memory_add": "memory",
    "memory_correction": "memory",
    "memory_delete": "memory",
    "proactive_config": "proactive",
    "drift_task_create": "drift",
    "dashboard_command": "dashboard",
    "plugin_management": "plugins",
}


@dataclass(slots=True)
class IntentDecision:
    intent: str
    confidence: float
    route: str
    entities: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "route": self.route,
            "entities": self.entities,
            "reason": self.reason,
        }

    def to_system_hint(self) -> str:
        entities = json.dumps(self.entities, ensure_ascii=False)
        return (
            "意图识别结果：\n"
            f"intent: {self.intent}\n"
            f"confidence: {self.confidence:.2f}\n"
            f"route: {self.route}\n"
            f"entities: {entities}\n"
            "说明：这是路由提示，不是最终事实。除非对回复有帮助，否则不要主动提及该分类。"
        )


class IntentRouter:
    def __init__(
        self,
        *,
        fast_llm_provider: LLMProvider | None = None,
        enabled: bool = True,
        llm_fallback_enabled: bool = True,
        fallback_confidence_threshold: float = 0.55,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.fast_llm_provider = fast_llm_provider
        self.enabled = enabled
        self.llm_fallback_enabled = llm_fallback_enabled
        self.fallback_confidence_threshold = fallback_confidence_threshold
        self.timeout_seconds = timeout_seconds

    async def classify(self, message: str) -> IntentDecision:
        if not self.enabled:
            return _decision(
                "general_chat",
                0.0,
                reason="Intent classification is disabled.",
            )

        decision = self._classify_by_rules(message)
        if (
            decision.confidence < self.fallback_confidence_threshold
            and self.llm_fallback_enabled
            and self.fast_llm_provider is not None
        ):
            return await self._classify_with_llm(message)
        return decision

    def _classify_by_rules(self, message: str) -> IntentDecision:
        text = message.casefold()

        if re.search(r"不是.+是", message) or _contains_any(
            text,
            ["更正", "纠正", "correction", "correct that", "actually"],
        ):
            return _decision(
                "memory_correction",
                0.92,
                entities={"text": message},
                reason="用户表达了更正或纠正已有信息的需求。",
            )
        if _contains_any(text, ["记住", "帮我记一下", "remember that", "please remember"]):
            return _decision(
                "memory_add",
                0.91,
                entities={"fact": message},
                reason="用户明确表达了记住信息的需求。",
            )
        if _contains_any(text, ["忘记", "忘掉", "别记了", "forget that", "delete memory"]):
            return _decision(
                "memory_delete",
                0.9,
                entities={"target": message},
                reason="用户表达了删除或遗忘记忆的需求。",
            )
        if _contains_any(
            text,
            [
                "以后别推",
                "不要提醒",
                "别主动发",
                "quiet hours",
                "push rule",
                "proactive",
                "notification rule",
            ],
        ):
            return _decision(
                "proactive_config",
                0.88,
                entities={"preference": message},
                reason="用户在调整主动推送或提醒规则。",
            )
        if _contains_any(text, ["空闲时", "后台", "drift", "when idle", "background task"]):
            return _decision(
                "drift_task_create",
                0.86,
                entities={"task": message},
                reason="用户提到了空闲或后台执行任务。",
            )
        if _contains_any(text, ["打开面板", "dashboard", "memory dashboard"]):
            return _decision(
                "dashboard_command",
                0.87,
                entities={"target": message},
                reason="用户想打开或查看 dashboard。",
            )
        if _contains_any(
            text,
            ["启用插件", "禁用插件", "plugin", "enable plugin", "disable plugin"],
        ):
            return _decision(
                "plugin_management",
                0.87,
                entities={"plugin_request": message},
                reason="用户表达了插件管理相关需求。",
            )
        if _contains_any(text, ["查一下", "搜索", "calculate", "tool:", "search for"]):
            return _decision(
                "tool_execution",
                0.84,
                entities={"request": message},
                reason="用户请求查询、搜索、计算或显式工具调用。",
            )
        return _decision(
            "general_chat",
            0.4,
            reason="未命中明确规则，按普通聊天处理。",
        )

    async def _classify_with_llm(self, message: str) -> IntentDecision:
        prompt = (
            "Classify the user message into exactly one Collie-agent intent.\n"
            f"Allowed intents: {', '.join(sorted(SUPPORTED_INTENTS))}\n"
            "Return strict JSON only with keys: intent, confidence, route, entities, reason.\n"
            "The result is only a routing hint and must not execute actions.\n"
            f"User message: {message}"
        )
        try:
            response = await self.fast_llm_provider.complete(
                [{"role": "user", "content": prompt}],
                temperature=0,
                timeout_seconds=self.timeout_seconds,
                purpose="intent_classification",
            )
            data = json.loads(response.strip())
        except Exception:
            return _decision(
                "general_chat",
                0.0,
                reason="Intent LLM fallback failed or returned invalid JSON.",
            )
        if not isinstance(data, dict):
            return _decision(
                "general_chat",
                0.0,
                reason="Intent LLM fallback returned a non-object JSON value.",
            )

        intent = str(data.get("intent", "general_chat"))
        if intent not in SUPPORTED_INTENTS:
            intent = "general_chat"
        confidence = _clamp_float(data.get("confidence", 0.0))
        route = str(data.get("route") or ROUTES[intent])
        entities = data.get("entities", {})
        if not isinstance(entities, dict):
            entities = {"value": entities}
        reason = str(data.get("reason", "Intent LLM fallback classification."))
        return IntentDecision(
            intent=intent,
            confidence=confidence,
            route=route,
            entities=entities,
            reason=reason,
        )


def _decision(
    intent: str,
    confidence: float,
    *,
    entities: dict[str, Any] | None = None,
    reason: str = "",
) -> IntentDecision:
    return IntentDecision(
        intent=intent,
        confidence=_clamp_float(confidence),
        route=ROUTES[intent],
        entities=entities or {},
        reason=reason,
    )


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle.casefold() in text for needle in needles)


def _clamp_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))
