from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from bootstrap.config import MemoryConfig
from memory.runtime import MemoryRuntime
from memory.server import create_memory_app


async def _client(tmp_path, config: MemoryConfig | None = None) -> TestClient:
    runtime = MemoryRuntime(tmp_path, config or MemoryConfig())
    await runtime.initialize()
    return TestClient(create_memory_app(runtime, runtime.config))


def _write_trace_jsonl(tmp_path, lines: list[object]) -> None:
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(
        line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)
        for line in lines
    )
    (trace_dir / "agent_traces.jsonl").write_text(payload + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_dashboard_route_returns_html_without_api_key(tmp_path) -> None:
    client = await _client(tmp_path, MemoryConfig(memory_server_api_key="secret"))

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="dashboard-root"' in response.text
    assert 'id="traceList"' in response.text
    assert 'id="traceDetail"' in response.text
    assert 'id="loadTracesButton"' in response.text
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_root_redirects_to_dashboard(tmp_path) -> None:
    client = await _client(tmp_path)

    response = client.get("/", follow_redirects=False)

    assert response.status_code in {302, 307}
    assert response.headers["location"] == "/dashboard"


@pytest.mark.asyncio
async def test_dashboard_static_assets_are_served(tmp_path) -> None:
    client = await _client(tmp_path)

    js = client.get("/static/dashboard.js")
    css = client.get("/static/dashboard.css")

    assert js.status_code == 200
    assert css.status_code == 200
    assert len(js.text.strip()) > 100
    assert len(css.text.strip()) > 100
    for function_name in [
        "apiFetch",
        "loadMemories",
        "loadStats",
        "saveMemory",
        "deleteMemory",
        "runOptimizer",
        "loadTraces",
        "loadTraceDetail",
        "renderTraceDetail",
    ]:
        assert f"function {function_name}" in js.text


@pytest.mark.asyncio
async def test_dashboard_does_not_bypass_api_auth(tmp_path) -> None:
    client = await _client(tmp_path, MemoryConfig(memory_server_api_key="secret"))

    dashboard = client.get("/dashboard")
    missing = client.get("/memory/stats")
    authorized = client.get("/memory/stats", headers={"Authorization": "Bearer secret"})

    assert dashboard.status_code == 200
    assert "secret" not in dashboard.text
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert authorized.status_code == 200


@pytest.mark.asyncio
async def test_trace_list_returns_empty_when_file_is_missing(tmp_path) -> None:
    client = await _client(tmp_path)

    response = client.get("/traces")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["limit"] == 20
    assert payload["skipped"] == 0
    assert payload["path_exists"] is False


@pytest.mark.asyncio
async def test_trace_list_uses_runtime_trace_config_path(tmp_path) -> None:
    trace_dir = tmp_path / "custom"
    trace_dir.mkdir()
    (trace_dir / "trace.jsonl").write_text(
        json.dumps({"trace_id": "configured", "started_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    runtime.trace_config = SimpleNamespace(path="custom/trace.jsonl")
    await runtime.initialize()
    client = TestClient(create_memory_app(runtime, runtime.config))

    response = client.get("/traces")

    assert response.status_code == 200
    assert response.json()["items"][0]["trace_id"] == "configured"


@pytest.mark.asyncio
async def test_trace_list_reads_jsonl_newest_first_and_limits(tmp_path) -> None:
    _write_trace_jsonl(
        tmp_path,
        [
            {
                "trace_id": "old",
                "session_id": "s1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "duration_ms": 10,
                "intent": {"intent": "general_chat", "confidence": 0.4, "route": "chat"},
                "finish_reason": "final_answer",
                "steps": [{"type": "llm"}, {"type": "tool"}],
                "memory_extracted_count": 0,
                "user_message_preview": "old message",
            },
            {
                "trace_id": "new",
                "session_id": "s2",
                "finished_at": "2026-01-03T00:00:00+00:00",
                "duration_ms": 30,
                "intent": {"intent": "tool_execution", "confidence": 0.9, "route": "tools"},
                "finish_reason": "final_answer",
                "steps": [{"type": "llm"}, {"type": "tool"}, {"type": "llm"}],
                "memory_extracted_count": 1,
                "user_message_preview": "new message",
            },
            {
                "trace_id": "middle",
                "session_id": "s3",
                "started_at": "2026-01-02T00:00:00+00:00",
                "duration_ms": 20,
                "steps": [],
            },
        ],
    )
    client = await _client(tmp_path)

    response = client.get("/traces?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert [item["trace_id"] for item in payload["items"]] == ["new", "middle"]
    assert payload["items"][0]["step_count"] == 3
    assert payload["items"][0]["tool_count"] == 1
    assert payload["items"][0]["intent"]["intent"] == "tool_execution"
    assert payload["limit"] == 2
    assert payload["path_exists"] is True


@pytest.mark.asyncio
async def test_trace_list_rejects_limit_above_max(tmp_path) -> None:
    client = await _client(tmp_path)

    response = client.get("/traces?limit=999")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_trace_list_skips_malformed_json_lines(tmp_path) -> None:
    _write_trace_jsonl(
        tmp_path,
        [
            {"trace_id": "ok", "started_at": "2026-01-01T00:00:00+00:00", "steps": []},
            "{not-json",
            ["not", "a", "dict"],
        ],
    )
    client = await _client(tmp_path)

    response = client.get("/traces")

    assert response.status_code == 200
    payload = response.json()
    assert [item["trace_id"] for item in payload["items"]] == ["ok"]
    assert payload["skipped"] == 2


@pytest.mark.asyncio
async def test_trace_detail_returns_full_trace(tmp_path) -> None:
    trace = {
        "trace_id": "trace-1",
        "session_id": "discord:123",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "duration_ms": 1000,
        "user_message_preview": "calculate",
        "intent": {"intent": "tool_execution", "confidence": 0.9, "route": "tools"},
        "memory_context_chars": 10,
        "prompt_message_count": 3,
        "steps": [
            {
                "type": "tool",
                "round": 0,
                "tool_name": "calculator",
                "arguments": {"expression": "1 + 2"},
                "result_preview": "3",
                "latency_ms": 1,
                "error": None,
            }
        ],
        "finish_reason": "final_answer",
        "memory_extracted_count": 0,
        "error": None,
    }
    _write_trace_jsonl(tmp_path, ["{broken", trace])
    client = await _client(tmp_path)

    response = client.get("/traces/trace-1")

    assert response.status_code == 200
    assert response.json()["trace"] == trace


@pytest.mark.asyncio
async def test_trace_detail_returns_404_when_missing(tmp_path) -> None:
    _write_trace_jsonl(tmp_path, [{"trace_id": "other"}])
    client = await _client(tmp_path)

    response = client.get("/traces/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_trace_api_uses_dashboard_api_key(tmp_path) -> None:
    client = await _client(tmp_path, MemoryConfig(memory_server_api_key="secret"))

    missing = client.get("/traces")
    authorized = client.get("/traces", headers={"Authorization": "Bearer secret"})

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert authorized.status_code == 200
