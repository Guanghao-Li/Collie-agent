from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, DefaultDict
import inspect
import logging


EventHandler = Callable[[object], object | Awaitable[object]]


@dataclass(slots=True)
class BaseEvent:
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StartupEvent(BaseEvent):
    pass


@dataclass(slots=True)
class ShutdownEvent(BaseEvent):
    pass


@dataclass(slots=True)
class BeforeTurnEvent(BaseEvent):
    session_id: str = ""
    user_message: str = ""


@dataclass(slots=True)
class PromptRenderEvent(BaseEvent):
    session_id: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class BeforeLLMEvent(BaseEvent):
    session_id: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class AfterLLMEvent(BaseEvent):
    session_id: str = ""
    response: str = ""


@dataclass(slots=True)
class ToolCallEvent(BaseEvent):
    session_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None


@dataclass(slots=True)
class AfterTurnEvent(BaseEvent):
    session_id: str = ""
    user_message: str = ""
    assistant_message: str = ""


@dataclass(slots=True)
class BeforeMemoryExtractEvent(BaseEvent):
    session_id: str = ""
    user_message: str = ""
    assistant_message: str = ""


@dataclass(slots=True)
class AfterMemoryExtractEvent(BaseEvent):
    session_id: str = ""
    extracted_count: int = 0


@dataclass(slots=True)
class BeforeProactivePushEvent(BaseEvent):
    candidate_id: str = ""
    message: str = ""


@dataclass(slots=True)
class AfterProactivePushEvent(BaseEvent):
    candidate_id: str = ""
    pushed: bool = False


@dataclass(slots=True)
class BeforeDriftTaskEvent(BaseEvent):
    task_name: str = ""


@dataclass(slots=True)
class AfterDriftTaskEvent(BaseEvent):
    task_name: str = ""
    success: bool = False
    summary: str = ""


class EventBus:
    def __init__(self) -> None:
        self._handlers: DefaultDict[type[object], list[EventHandler]] = defaultdict(list)
        self._logger = logging.getLogger(__name__)

    def subscribe(self, event_type: type[object], handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: object) -> object:
        handlers = list(self._handlers.get(type(event), []))
        handlers.extend(self._handlers.get(BaseEvent, []))
        for handler in handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                self._logger.exception("事件处理器执行失败：%s", type(event).__name__)
        return event
