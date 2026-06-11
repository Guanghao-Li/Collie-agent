from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from bootstrap.config import MemoryConfig
from memory.runtime import MemoryRuntime
from memory.server import create_memory_app


async def _client(tmp_path, config: MemoryConfig | None = None) -> TestClient:
    runtime = MemoryRuntime(tmp_path, config or MemoryConfig())
    await runtime.initialize()
    return TestClient(create_memory_app(runtime, runtime.config))


@pytest.mark.asyncio
async def test_dashboard_route_returns_html_without_api_key(tmp_path) -> None:
    client = await _client(tmp_path, MemoryConfig(memory_server_api_key="secret"))

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="dashboard-root"' in response.text
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
