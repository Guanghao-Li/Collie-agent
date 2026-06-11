from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from bootstrap.config import MemoryConfig
from memory.models import OptimizationResult
from memory.optimizer import MemoryOptimizer


class MemoryOptimizerScheduler:
    def __init__(
        self,
        *,
        config: MemoryConfig,
        state_path: str | Path | None = None,
    ) -> None:
        self.config = config
        self.state_path = Path(
            state_path
            or getattr(config, "optimizer_state_path", ".collie/memory/optimizer_state.json")
        )

    def should_run(self, pending_count: int, now: datetime | None = None) -> bool:
        if not bool(getattr(self.config, "optimizer_auto_run", False)):
            return False
        if int(pending_count) < int(getattr(self.config, "optimizer_min_pending", 1)):
            return False
        now = _ensure_aware(now or datetime.now(timezone.utc))
        last_run = _parse_datetime(self.read_state().get("last_run_at"))
        if last_run is None:
            return True
        interval = int(getattr(self.config, "optimizer_interval_seconds", 64800))
        elapsed = (now - last_run).total_seconds()
        return elapsed >= interval

    async def run_if_due(
        self,
        optimizer: MemoryOptimizer,
        *,
        pending_count: int,
        now: datetime | None = None,
    ) -> OptimizationResult | None:
        now = _ensure_aware(now or datetime.now(timezone.utc))
        if not self.should_run(pending_count, now):
            return None
        try:
            result = await optimizer.optimize()
        except Exception as exc:
            self.write_state(
                {
                    **self.read_state(),
                    "last_run_at": now.isoformat(),
                    "last_result": {},
                    "last_error": str(exc),
                }
            )
            return None
        self.write_state(
            {
                "last_run_at": now.isoformat(),
                "last_result": _result_to_dict(result),
                "last_error": "" if result.ok else "; ".join(result.errors),
            }
        )
        return result

    def read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _result_to_dict(result: OptimizationResult) -> dict[str, Any]:
    if is_dataclass(result):
        return asdict(result)
    return {
        "ok": getattr(result, "ok", False),
        "summary": getattr(result, "summary", ""),
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_aware(parsed)


def _ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
