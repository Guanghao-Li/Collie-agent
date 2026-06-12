from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
import hmac
import json

from pydantic import BaseModel, Field

from bootstrap.config import MemoryConfig
from memory.engine import MemoryMutation, MemoryQuery, MemoryQueryFilters
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


class BatchDeleteRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    reason: str = ""


class DeleteRequest(BaseModel):
    reason: str = ""


class FindSimilarRequest(BaseModel):
    id: str | None = None
    text: str | None = None
    limit: int = 10


class RecallRequest(BaseModel):
    query: str
    intent: str = "answer"
    memory_kind: str | None = None
    limit: int = 8


class MemorizeRequest(BaseModel):
    summary: str
    memory_kind: str = "preference"
    importance: float = 0.7
    confidence: float = 0.8
    source_ref: str = "memory_server"


class OptimizeRequest(BaseModel):
    force: bool = False


def create_memory_app(runtime: MemoryRuntime, config: MemoryConfig):
    try:
        from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse
        from fastapi.responses import JSONResponse
        from fastapi.responses import RedirectResponse
    except ImportError as exc:  # pragma: no cover - exercised when optional deps are absent
        raise RuntimeError(
            "memory web API requires optional dependencies: fastapi and uvicorn"
        ) from exc

    app = FastAPI(title="Collie Memory API", version="0.1.0")
    cors_origins = list(getattr(config, "memory_server_cors_origins", []) or [])
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[str(origin) for origin in cors_origins],
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        )

    static_dir = _dashboard_static_dir()
    app.mount(
        "/static",
        StaticFiles(directory=static_dir, check_dir=False),
        name="static",
    )

    def admin_service():
        service = getattr(runtime.engine, "admin_service", None)
        if service is None:
            raise ApiError(500, "internal_error", "Memory admin service is unavailable")
        return service

    async def require_api_key(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        expected = str(getattr(config, "memory_server_api_key", "") or "")
        if not expected:
            return
        provided = ""
        if authorization and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        elif x_api_key:
            provided = x_api_key.strip()
        if not provided:
            raise ApiError(401, "unauthorized", "API key is required")
        if not hmac.compare_digest(provided, expected):
            raise ApiError(403, "forbidden", "API key is invalid")

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):  # noqa: ARG001
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(exc.code, exc.message),
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):  # noqa: ARG001
        code = {
            400: "invalid_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
        }.get(exc.status_code, "internal_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(code, str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):  # noqa: ARG001
        return JSONResponse(
            status_code=400,
            content=_error_payload("invalid_request", "Invalid request"),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):  # noqa: ARG001
        return JSONResponse(
            status_code=500,
            content=_error_payload("internal_error", "Internal server error"),
        )

    @app.get("/", include_in_schema=False)
    async def dashboard_root():
        return RedirectResponse(url="/dashboard")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page():
        dashboard_html = static_dir / "dashboard.html"
        if not dashboard_html.exists():
            raise HTTPException(status_code=404, detail="dashboard asset not found")
        return FileResponse(dashboard_html, media_type="text/html")

    @app.get("/health")
    async def health() -> dict[str, object]:
        vector_store = getattr(runtime.engine, "vector_store", None)
        vector_is_enabled = getattr(vector_store, "is_enabled", None)
        return {
            "ok": True,
            "service": "memory",
            "vector_enabled": bool(vector_is_enabled() if callable(vector_is_enabled) else False),
            "admin_enabled": getattr(runtime.engine, "admin_service", None) is not None,
        }

    @app.get("/memory/stats", dependencies=[Depends(require_api_key)])
    async def memory_stats() -> dict[str, Any]:
        return await admin_service().get_stats()

    @app.get("/memory/events", dependencies=[Depends(require_api_key)])
    async def memory_events(
        start: str | None = None,
        end: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return await admin_service().list_event_range(start=start, end=end, limit=limit)

    @app.post("/memory/batch-delete", dependencies=[Depends(require_api_key)])
    async def memory_batch_delete(request: BatchDeleteRequest) -> dict[str, Any]:
        if not request.ids:
            raise ApiError(400, "invalid_request", "ids is required")
        return await admin_service().batch_delete(request.ids, reason=request.reason)

    @app.post("/memory/find-similar", dependencies=[Depends(require_api_key)])
    async def memory_find_similar(request: FindSimilarRequest) -> dict[str, Any]:
        if not request.id and not request.text:
            raise ApiError(400, "invalid_request", "id or text is required")
        return await admin_service().find_similar(
            memory_id=request.id,
            text=request.text,
            limit=request.limit,
        )

    @app.post("/memory/recall", dependencies=[Depends(require_api_key)])
    async def memory_recall(request: RecallRequest) -> dict[str, Any]:
        allowed_intents = {"answer", "context", "timeline", "procedure", "interest"}
        if request.intent not in allowed_intents:
            raise ApiError(400, "invalid_request", "unsupported recall intent")
        intent = request.intent
        result = await runtime.engine.query(
            MemoryQuery(
                intent=intent,  # type: ignore[arg-type]
                text=request.query,
                limit=request.limit,
                filters=MemoryQueryFilters(
                    kinds=(request.memory_kind,) if request.memory_kind else (),
                ),
            )
        )
        return {
            "content": result.content,
            "text_block": result.text_block,
            "records": [
                {
                    "id": record.id,
                    "kind": record.kind,
                    "summary": record.summary,
                    "score": record.score,
                    "injected": record.injected,
                }
                for record in result.records
            ],
            "metadata": result.metadata,
        }

    @app.post("/memory/memorize", dependencies=[Depends(require_api_key)])
    async def memory_memorize(request: MemorizeRequest) -> dict[str, Any]:
        result = await runtime.engine.mutate(
            MemoryMutation(
                kind="remember",
                summary=request.summary,
                memory_kind=request.memory_kind,
                source_ref=request.source_ref,
                stable=True,
                metadata={
                    "importance": request.importance,
                    "confidence": request.confidence,
                },
            )
        )
        return _mutation_result_to_dict(result)

    @app.post("/memory/optimize", dependencies=[Depends(require_api_key)])
    async def memory_optimize(request: OptimizeRequest | None = None) -> dict[str, Any]:  # noqa: ARG001
        result = await runtime.optimize_pending()
        return _result_to_dict(result)

    @app.get("/memory/optimizer/state", dependencies=[Depends(require_api_key)])
    async def memory_optimizer_state() -> dict[str, Any]:
        scheduler = getattr(runtime, "scheduler", None)
        return scheduler.read_state() if scheduler is not None else {}

    @app.get("/traces", dependencies=[Depends(require_api_key)])
    async def trace_list(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        trace_path = _resolve_trace_path(runtime, config)
        items, skipped = _read_trace_jsonl(trace_path)
        summaries = [_trace_summary(item) for item in _sort_traces_newest_first(items)]
        return {
            "items": summaries[:limit],
            "limit": limit,
            "skipped": skipped,
            "path_exists": trace_path.exists(),
        }

    @app.get("/traces/{trace_id}", dependencies=[Depends(require_api_key)])
    async def trace_detail(trace_id: str) -> dict[str, Any]:
        trace_path = _resolve_trace_path(runtime, config)
        if not trace_path.exists():
            raise ApiError(404, "not_found", "Trace file not found")
        items, _skipped = _read_trace_jsonl(trace_path)
        for item in items:
            if str(item.get("trace_id", "")) == trace_id:
                return {"trace": item}
        raise ApiError(404, "not_found", "Trace not found")

    @app.get("/memory", dependencies=[Depends(require_api_key)])
    async def memory_list(
        kind: str | None = None,
        status: str = "active",
        query: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return await admin_service().list_dashboard(
            limit=limit,
            offset=offset,
            kind=kind,
            status=status,
            query=query,
        )

    @app.get("/memory/{memory_id}", dependencies=[Depends(require_api_key)])
    async def memory_detail(memory_id: str) -> dict[str, Any]:
        detail = await admin_service().get_dashboard_detail(memory_id)
        if detail is None:
            raise ApiError(404, "not_found", "Memory not found")
        return detail

    @app.patch("/memory/{memory_id}", dependencies=[Depends(require_api_key)])
    async def memory_update(
        memory_id: str,
        fields: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        forbidden = {"id", "embedding", "embedding_json", "api_key"}
        blocked = sorted(set(fields) & forbidden)
        if blocked:
            raise ApiError(
                400,
                "invalid_request",
                "unsupported update fields: " + ", ".join(blocked),
            )
        try:
            return await admin_service().update_dashboard_memory(memory_id, fields)
        except KeyError:
            raise ApiError(404, "not_found", "Memory not found") from None
        except ValueError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc

    @app.delete("/memory/{memory_id}", dependencies=[Depends(require_api_key)])
    async def memory_delete(
        memory_id: str,
        request: DeleteRequest | None = Body(default=None),
    ) -> dict[str, Any]:
        result = await admin_service().delete_dashboard_memory(
            memory_id,
            reason=request.reason if request else "",
        )
        if not result.get("affected_ids"):
            raise ApiError(404, "not_found", "Memory not found")
        return result

    return app


async def run_memory_server(runtime: MemoryRuntime, config: MemoryConfig) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - optional runtime path
        raise RuntimeError(
            "memory web API requires optional dependency: uvicorn"
        ) from exc
    app = create_memory_app(runtime, config)
    uvicorn_config = uvicorn.Config(
        app,
        host=str(getattr(config, "memory_server_host", "127.0.0.1")),
        port=int(getattr(config, "memory_server_port", 8765)),
        log_level="info",
    )
    server = uvicorn.Server(uvicorn_config)
    await server.serve()


def _error_payload(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _dashboard_static_dir() -> Path:
    return Path(__file__).with_name("static")


def _resolve_trace_path(runtime: MemoryRuntime, config: MemoryConfig) -> Path:
    trace_config = getattr(runtime, "trace_config", None) or getattr(config, "trace", None)
    raw_path = getattr(trace_config, "path", None) or getattr(
        config,
        "trace_path",
        "traces/agent_traces.jsonl",
    )
    trace_path = Path(str(raw_path))
    if trace_path.is_absolute():
        return trace_path
    return Path(getattr(runtime, "workspace", Path("."))) / trace_path


def _read_trace_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    items: list[dict[str, Any]] = []
    skipped = 0
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(item, dict):
                skipped += 1
                continue
            items.append(item)
    return items, skipped


def _sort_traces_newest_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: str(item.get("finished_at") or item.get("started_at") or ""),
        reverse=True,
    )


def _trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
    steps = trace.get("steps", [])
    if not isinstance(steps, list):
        steps = []
    return {
        "trace_id": trace.get("trace_id", ""),
        "session_id": trace.get("session_id", ""),
        "started_at": trace.get("started_at"),
        "finished_at": trace.get("finished_at"),
        "duration_ms": trace.get("duration_ms"),
        "intent": trace.get("intent") if isinstance(trace.get("intent"), dict) else None,
        "finish_reason": trace.get("finish_reason", ""),
        "step_count": len(steps),
        "tool_count": sum(1 for step in steps if isinstance(step, dict) and step.get("type") == "tool"),
        "memory_extracted_count": trace.get("memory_extracted_count", 0),
        "user_message_preview": trace.get("user_message_preview", ""),
    }


def _result_to_dict(result: Any) -> dict[str, Any]:
    if is_dataclass(result):
        return asdict(result)
    if isinstance(result, dict):
        return dict(result)
    return {
        "ok": bool(getattr(result, "ok", False)),
        "summary": str(getattr(result, "summary", "")),
    }


def _mutation_result_to_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        payload = dict(result)
        item = payload.get("item")
        if isinstance(item, MemoryItem):
            payload["item"] = item.to_dict()
        if "ok" not in payload:
            payload["ok"] = bool(payload.get("accepted", False))
        if "accepted" not in payload:
            payload["accepted"] = bool(payload.get("ok", False))
        return payload

    payload = _result_to_dict(result)
    payload.update(
        {
            "ok": bool(getattr(result, "ok", False)),
            "accepted": bool(getattr(result, "accepted", False)),
            "item_id": str(getattr(result, "item_id", "")),
            "actual_kind": str(getattr(result, "actual_kind", "")),
            "status": str(getattr(result, "status", "")),
            "affected_ids": list(getattr(result, "affected_ids", [])),
            "missing_ids": list(getattr(result, "missing_ids", [])),
        }
    )
    item = getattr(result, "item", None)
    if isinstance(item, MemoryItem):
        payload["item"] = item.to_dict()
    return payload
