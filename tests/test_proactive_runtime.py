from __future__ import annotations

import pytest

from bootstrap.app import build_app_runtime
from bootstrap.config import Settings
from proactive.judge import ProactiveJudge
from proactive.sources import ManualCandidateSource


class FakePrefilterProvider:
    def __init__(self, response: str) -> None:
        self.name = "fake-fast"
        self.model = "fake-fast"
        self.response = response
        self.calls = 0

    async def complete(self, messages, *, temperature=None, timeout_seconds=None, purpose=None):
        self.calls += 1
        return self.response

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_proactive_runtime_pushes_manual_candidate(tmp_path) -> None:
    config = Settings()
    config.proactive.quiet_hours_start = "00:00"
    config.proactive.quiet_hours_end = "00:00"
    config.proactive.min_score_to_push = 0.1
    config.discord.default_push_channel_id = "push-channel"
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    source = ManualCandidateSource()
    source.add_candidate("项目提醒", "跟进 Agent 项目。")
    await runtime.proactive_runtime.add_source(source)

    decisions = await runtime.proactive_runtime.check_once()
    outbound = await runtime.message_bus.receive_outbound()

    assert decisions[0].should_push is True
    assert outbound.session_id == "push-channel"
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_proactive_runtime_respects_daily_limit(tmp_path) -> None:
    config = Settings()
    config.proactive.quiet_hours_start = "00:00"
    config.proactive.quiet_hours_end = "00:00"
    config.proactive.max_pushes_per_day = 0
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    source = ManualCandidateSource()
    source.add_candidate("项目提醒", "跟进。")
    await runtime.proactive_runtime.add_source(source)

    assert await runtime.proactive_runtime.check_once() == []
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_proactive_fast_prefilter_low_score_skips_main_judge(tmp_path) -> None:
    config = Settings()
    config.proactive.quiet_hours_start = "00:00"
    config.proactive.quiet_hours_end = "00:00"
    config.proactive.fast_prefilter_enabled = True
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    fast = FakePrefilterProvider('{"relevant": false, "rough_score": 0.1, "reason": "无关"}')
    judge = ProactiveJudge(runtime.llm_provider, runtime.memory_runtime, fast)
    runtime.proactive_runtime.judge = judge
    source = ManualCandidateSource()
    source.add_candidate("无关提醒", "随便看看")
    await runtime.proactive_runtime.add_source(source)

    decisions = await runtime.proactive_runtime.check_once()

    assert decisions[0].should_push is False
    assert judge.main_judge_calls == 0
    assert fast.calls == 1
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_proactive_fast_prefilter_high_score_calls_main_judge(tmp_path) -> None:
    config = Settings()
    config.proactive.quiet_hours_start = "00:00"
    config.proactive.quiet_hours_end = "00:00"
    config.proactive.fast_prefilter_enabled = True
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    fast = FakePrefilterProvider('{"relevant": true, "rough_score": 0.9, "reason": "相关"}')
    judge = ProactiveJudge(runtime.llm_provider, runtime.memory_runtime, fast)
    runtime.proactive_runtime.judge = judge
    source = ManualCandidateSource()
    source.add_candidate("项目提醒", "跟进 Agent 项目。")
    await runtime.proactive_runtime.add_source(source)

    await runtime.proactive_runtime.check_once()

    assert judge.main_judge_calls == 1
    assert fast.calls == 1
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_proactive_prefilter_disabled_uses_original_flow(tmp_path) -> None:
    config = Settings()
    config.proactive.quiet_hours_start = "00:00"
    config.proactive.quiet_hours_end = "00:00"
    config.proactive.fast_prefilter_enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    fast = FakePrefilterProvider('{"relevant": false, "rough_score": 0.1, "reason": "无关"}')
    judge = ProactiveJudge(runtime.llm_provider, runtime.memory_runtime, fast)
    runtime.proactive_runtime.judge = judge
    source = ManualCandidateSource()
    source.add_candidate("项目提醒", "跟进 Agent 项目。")
    await runtime.proactive_runtime.add_source(source)

    await runtime.proactive_runtime.check_once()

    assert judge.main_judge_calls == 1
    assert fast.calls == 0
    await runtime.llm_provider.close()
