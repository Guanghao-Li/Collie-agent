from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.llm import LLMProvider
from agent.models import CommandResult
from drift.runtime import DriftRuntime
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime
from proactive.runtime import ProactiveRuntime
from session.manager import SessionManager
from tools.registry import ToolRegistry


@dataclass(slots=True)
class AgentCommands:
    session_manager: SessionManager
    memory_runtime: MemoryRuntime
    tool_registry: ToolRegistry
    llm_provider: LLMProvider
    main_llm_provider: LLMProvider | None = None
    fast_llm_provider: LLMProvider | None = None
    proactive_runtime: ProactiveRuntime | None = None
    drift_runtime: DriftRuntime | None = None
    runtime_state: dict[str, Any] | None = None

    async def handle(self, session_id: str, content: str) -> CommandResult:
        if content.startswith("!ask "):
            return CommandResult(handled=False, replacement_content=content[5:].strip())
        if not content.startswith("!"):
            return CommandResult(handled=False)

        command, _, arg = content.partition(" ")
        command = command.lower()
        arg = arg.strip()

        if command == "!help":
            return CommandResult(True, _help_text())
        if command == "!status":
            return CommandResult(True, await self._status())
        if command == "!remember":
            return CommandResult(True, await self._remember(arg))
        if command == "!memory":
            return CommandResult(True, await self._memory(arg))
        if command == "!forget":
            return CommandResult(True, await self._forget(arg))
        if command == "!drift":
            return CommandResult(True, await self._drift())
        if command == "!proactive":
            return CommandResult(True, await self._proactive())
        if command == "!clear":
            self.session_manager.clear_session(session_id)
            return CommandResult(True, "当前会话历史已清空。")
        return CommandResult(True, "未知命令。可以输入 !help 查看帮助。")

    async def _status(self) -> str:
        memory_stats = await self.memory_runtime.stats()
        runtime_state = self.runtime_state or {}
        proactive_count = (
            self.proactive_runtime.daily_push_count if self.proactive_runtime is not None else 0
        )
        drift_last = (
            self.drift_runtime.last_run_at.isoformat(timespec="seconds")
            if self.drift_runtime is not None and self.drift_runtime.last_run_at
            else "从未运行"
        )
        main_provider = self.main_llm_provider or self.llm_provider
        fast_provider = self.fast_llm_provider or main_provider
        proactive_fast_enabled = (
            self.proactive_runtime is not None
            and self.proactive_runtime.config.proactive.fast_prefilter_enabled
        )
        return (
            f"Runtime 运行中：{runtime_state.get('running', False)}\n"
            f"活跃记忆：{memory_stats['active']}；待整理记忆：{memory_stats['pending']}\n"
            f"今日主动推送次数：{proactive_count}\n"
            f"上次 Drift 运行时间：{drift_last}\n"
            f"LLM main：{getattr(main_provider, 'name', 'unknown')} / "
            f"{getattr(main_provider, 'model', 'unknown')}\n"
            f"LLM fast：{'enabled' if self.fast_llm_provider is not None else 'disabled'} / "
            f"{getattr(fast_provider, 'model', getattr(fast_provider, 'name', 'unknown'))}\n"
            f"Fast fallback 到 main：{fast_provider is main_provider}\n"
            f"Memory gate：enabled\n"
            f"Query rewrite：enabled\n"
            f"HyDE：{'enabled' if self.memory_runtime.config.enable_hyde else 'disabled'}\n"
            f"Proactive fast prefilter：{'enabled' if proactive_fast_enabled else 'disabled'}"
        )

    async def _remember(self, text: str) -> str:
        if not text:
            return "用法：!remember <内容>"
        item = MemoryItem(
            type="fact",
            text=text,
            tags=["manual"],
            importance=0.8,
            confidence=0.9,
            source="command:remember",
            status="pending",
        )
        await self.memory_runtime.append_pending_memory(item)
        return f"已加入待整理记忆：{text}"

    async def _memory(self, arg: str) -> str:
        if arg.startswith("search "):
            query = arg.removeprefix("search ").strip()
        elif arg.startswith("搜索 "):
            query = arg.removeprefix("搜索 ").strip()
        else:
            query = ""
        if query:
            results = await self.memory_runtime.search(query)
            if not results:
                return "没有找到匹配的记忆。"
            return "\n".join(f"- {item.text} ({item.id})" for item in results)
        profile = await self.memory_runtime.read_profile()
        memory = await self.memory_runtime.read_core_memory()
        combined = f"{profile.strip()}\n\n{memory.strip()}".strip()
        return combined[:1800] if combined else "记忆为空。"

    async def _forget(self, keyword: str) -> str:
        if not keyword:
            return "用法：!forget <关键词>"
        matches = await self.memory_runtime.search(keyword, limit=10)
        if not matches:
            return "没有找到可遗忘的匹配记忆。"
        for item in matches:
            await self.memory_runtime.delete_memory(item.id, f"按关键词遗忘：{keyword}")
        return f"已软删除 {len(matches)} 条匹配记忆。"

    async def _drift(self) -> str:
        if self.drift_runtime is None:
            return "Drift runtime 不可用。"
        results = await self.drift_runtime.run_once(force=True)
        if not results:
            return "没有 Drift 任务被执行。"
        return "\n".join(f"- {result.task_name}: {result.summary}" for result in results)

    async def _proactive(self) -> str:
        if self.proactive_runtime is None:
            return "Proactive runtime 不可用。"
        decisions = await self.proactive_runtime.check_once()
        if not decisions:
            return "没有找到可主动推送的候选内容。"
        return "\n".join(
            f"- {decision.score:.2f} 是否推送={decision.should_push}：{decision.reason}"
            for decision in decisions
        )


def _help_text() -> str:
    return (
        "命令：\n"
        "!ask <内容> - 与 Agent 对话\n"
        "!remember <内容> - 写入一条待整理的长期记忆\n"
        "!memory - 查看用户画像和核心记忆\n"
        "!memory search <查询> 或 !memory 搜索 <查询> - 搜索长期记忆\n"
        "!forget <关键词> - 软删除匹配的记忆\n"
        "!drift - 手动运行一次 Drift cycle\n"
        "!proactive - 手动运行一次主动推送检查\n"
        "!status - 查看 runtime 状态\n"
        "!clear - 清空当前会话\n"
        "!help - 显示命令帮助"
    )
