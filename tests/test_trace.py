from __future__ import annotations

import json

from agent.trace import TraceRecorder


class NotJsonSerializable:
    pass


def test_trace_recorder_writes_jsonl(tmp_path) -> None:
    recorder = TraceRecorder(tmp_path)
    trace = recorder.start_trace("s1", "hello")
    assert trace is not None
    trace.intent = {"intent": "general_chat"}
    trace.memory_context_chars = 12
    trace.prompt_message_count = 2
    trace.finish_reason = "final_answer"
    trace.memory_extracted_count = 1
    recorder.record_llm_step(
        trace,
        round_index=0,
        purpose="agent_loop",
        latency_ms=3,
        response="hi",
        has_tool_call=False,
        tool_name=None,
    )

    recorder.write(trace)

    lines = (tmp_path / "traces" / "agent_traces.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["trace_id"]
    assert record["session_id"] == "s1"
    assert record["duration_ms"] >= 0
    assert record["steps"][0]["type"] == "llm"
    assert "_started_monotonic" not in record


def test_trace_preview_truncates_to_max_chars(tmp_path) -> None:
    recorder = TraceRecorder(tmp_path, max_preview_chars=8)

    assert recorder.preview("abcdefghijklmnopqrstuvwxyz") == "abcde..."


def test_trace_recorder_safely_converts_non_json_values(tmp_path) -> None:
    recorder = TraceRecorder(tmp_path)
    trace = recorder.start_trace("s1", "hello")
    assert trace is not None
    recorder.record_tool_step(
        trace,
        round_index=0,
        tool_name="odd_tool",
        arguments={"value": NotJsonSerializable()},
        result={"items": [NotJsonSerializable()]},
        latency_ms=5,
    )

    recorder.write(trace)

    line = (tmp_path / "traces" / "agent_traces.jsonl").read_text(encoding="utf-8")
    record = json.loads(line)
    step = record["steps"][0]
    assert isinstance(step["arguments"]["value"], str)
    assert isinstance(json.loads(step["result_preview"])["items"][0], str)


def test_trace_write_failure_does_not_raise(tmp_path) -> None:
    recorder = TraceRecorder(tmp_path, path=tmp_path)
    trace = recorder.start_trace("s1", "hello")

    recorder.write(trace)
