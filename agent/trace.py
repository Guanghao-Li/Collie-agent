from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import logging
import time
import uuid


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview(value: Any, max_chars: int) -> str:
    text = value if isinstance(value, str) else _safe_json_dumps(_to_json_safe(value))
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return f"{text[: max_chars - 3]}..."


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _to_json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        pass

    if isinstance(value, dict):
        return {str(key): _to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(item) for item in value]
    return str(value)


@dataclass(slots=True)
class TraceStep:
    type: str
    round: int
    purpose: str | None = None
    latency_ms: int = 0
    response_preview: str = ""
    has_tool_call: bool = False
    tool_name: str | None = None
    arguments: Any = None
    result_preview: str = ""
    error: str | None = None


@dataclass(slots=True)
class AgentTrace:
    trace_id: str
    session_id: str
    started_at: str
    user_message_preview: str
    finished_at: str | None = None
    duration_ms: int | None = None
    intent: dict[str, Any] | None = None
    memory_context_chars: int = 0
    prompt_message_count: int = 0
    steps: list[TraceStep] = field(default_factory=list)
    finish_reason: str = ""
    memory_extracted_count: int = 0
    error: str | None = None
    _started_monotonic: float = field(default_factory=time.perf_counter, repr=False)

    def finish(self) -> None:
        self.finished_at = _utc_now_iso()
        self.duration_ms = int((time.perf_counter() - self._started_monotonic) * 1000)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record.pop("_started_monotonic", None)
        return record


class TraceRecorder:
    def __init__(
        self,
        workspace: str | Path,
        path: str | Path = "traces/agent_traces.jsonl",
        *,
        enabled: bool = True,
        max_preview_chars: int = 500,
    ) -> None:
        self.enabled = enabled
        self.max_preview_chars = max_preview_chars
        trace_path = Path(path)
        self.path = trace_path if trace_path.is_absolute() else Path(workspace) / trace_path
        self._logger = logging.getLogger(__name__)

    def start_trace(self, session_id: str, user_message: str) -> AgentTrace | None:
        if not self.enabled:
            return None
        return AgentTrace(
            trace_id=str(uuid.uuid4()),
            session_id=session_id,
            started_at=_utc_now_iso(),
            user_message_preview=self.preview(user_message),
        )

    def preview(self, value: Any) -> str:
        return _preview(value, self.max_preview_chars)

    def json_safe(self, value: Any) -> Any:
        return _to_json_safe(value)

    def record_llm_step(
        self,
        trace: AgentTrace | None,
        *,
        round_index: int,
        purpose: str,
        latency_ms: int,
        response: str,
        has_tool_call: bool,
        tool_name: str | None,
    ) -> None:
        if trace is None:
            return
        trace.steps.append(
            TraceStep(
                type="llm",
                round=round_index,
                purpose=purpose,
                latency_ms=latency_ms,
                response_preview=self.preview(response),
                has_tool_call=has_tool_call,
                tool_name=tool_name,
            )
        )

    def record_tool_step(
        self,
        trace: AgentTrace | None,
        *,
        round_index: int,
        tool_name: str,
        arguments: Any,
        result: Any,
        latency_ms: int,
        error: str | None = None,
    ) -> None:
        if trace is None:
            return
        trace.steps.append(
            TraceStep(
                type="tool",
                round=round_index,
                tool_name=tool_name,
                arguments=self.json_safe(arguments),
                result_preview=self.preview(result),
                latency_ms=latency_ms,
                error=self.preview(error) if error else None,
            )
        )

    def write(self, trace: AgentTrace | None) -> None:
        if trace is None or not self.enabled:
            return
        try:
            if trace.finished_at is None:
                trace.finish()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(trace.to_record(), ensure_ascii=False) + "\n")
        except Exception:
            self._logger.exception("Trace 写入失败：%s", self.path)
