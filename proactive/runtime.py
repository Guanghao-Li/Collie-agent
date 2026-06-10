from __future__ import annotations

import asyncio
import contextlib
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo
import logging

from bootstrap.config import Settings
from bus.event_bus import AfterProactivePushEvent, BeforeProactivePushEvent, EventBus
from bus.message_bus import MessageBus
from bus.models import OutboundMessage
from memory.runtime import MemoryRuntime
from proactive.judge import ProactiveJudge
from proactive.models import ProactiveDecision
from proactive.sources import ProactiveSource, ProactiveSourceRegistry


class ProactiveRuntime:
    def __init__(
        self,
        config: Settings,
        source_registry: ProactiveSourceRegistry,
        judge: ProactiveJudge,
        memory_runtime: MemoryRuntime,
        message_bus: MessageBus,
        event_bus: EventBus,
    ) -> None:
        self.config = config
        self.source_registry = source_registry
        self.judge = judge
        self.memory_runtime = memory_runtime
        self.message_bus = message_bus
        self.event_bus = event_bus
        self.last_push_times: list[datetime] = []
        self.daily_push_count = 0
        self._daily_date: date | None = None
        self._pushed_candidate_ids: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._logger = logging.getLogger(__name__)

    async def start(self) -> None:
        if self._running or not self.config.proactive.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self.run_loop(), name="proactive-runtime")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_loop(self) -> None:
        while self._running:
            try:
                await self.check_once()
            except Exception:
                self._logger.exception("主动推送检查失败")
            await asyncio.sleep(self.config.proactive.interval_seconds)

    async def check_once(self) -> list[ProactiveDecision]:
        self._reset_daily_count_if_needed()
        decisions: list[ProactiveDecision] = []
        if self._is_quiet_hours():
            return decisions
        if self.daily_push_count >= self.config.proactive.max_pushes_per_day:
            return decisions

        for source in self.source_registry.list_sources():
            candidates = await source.fetch()
            for candidate in candidates:
                if self.config.proactive.fast_prefilter_enabled:
                    prefilter = await self.judge.fast_prefilter(
                        candidate,
                        self.config.proactive.fast_prefilter_min_score,
                    )
                    if not prefilter.relevant:
                        decisions.append(
                            ProactiveDecision(
                                candidate=candidate,
                                should_push=False,
                                score=prefilter.rough_score,
                                reason=f"fast_prefilter_rejected: {prefilter.reason}",
                                message="",
                            )
                        )
                        continue
                decision = await self.judge.judge(
                    candidate,
                    self.config.proactive.min_score_to_push,
                    list(self._pushed_candidate_ids),
                )
                decisions.append(decision)
                if decision.should_push and self.daily_push_count < self.config.proactive.max_pushes_per_day:
                    await self.push(decision)
        return decisions

    async def push(self, decision: ProactiveDecision) -> None:
        await self.event_bus.publish(
            BeforeProactivePushEvent(
                candidate_id=decision.candidate.id,
                message=decision.message,
            )
        )
        target = self.config.discord.default_push_channel_id or "default"
        await self.message_bus.publish_outbound(
            OutboundMessage(
                channel="discord",
                session_id=target,
                content=decision.message,
                metadata={"proactive": True, "candidate_id": decision.candidate.id},
            )
        )
        self._pushed_candidate_ids.add(decision.candidate.id)
        self.last_push_times.append(datetime.now(timezone.utc))
        self.daily_push_count += 1
        await self.event_bus.publish(
            AfterProactivePushEvent(candidate_id=decision.candidate.id, pushed=True)
        )

    async def add_source(self, source: ProactiveSource) -> None:
        self.source_registry.register(source)

    def _reset_daily_count_if_needed(self) -> None:
        today = datetime.now(ZoneInfo(self.config.app.timezone)).date()
        if self._daily_date != today:
            self._daily_date = today
            self.daily_push_count = 0

    def _is_quiet_hours(self) -> bool:
        now = datetime.now(ZoneInfo(self.config.app.timezone)).time()
        start = _parse_time(self.config.proactive.quiet_hours_start)
        end = _parse_time(self.config.proactive.quiet_hours_end)
        if start == end:
            return False
        if start < end:
            return start <= now < end
        return now >= start or now < end


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))
