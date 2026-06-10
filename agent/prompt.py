from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bootstrap.config import Settings
from session.models import SessionMessage
from tools.registry import ToolRegistry


class PromptBuilder:
    def __init__(self, config: Settings, tool_registry: ToolRegistry) -> None:
        self.config = config
        self.tool_registry = tool_registry

    def build(
        self,
        user_message: str,
        recent_messages: list[SessionMessage],
        memory_context: str,
    ) -> list[dict[str, str]]:
        now = datetime.now(ZoneInfo(self.config.app.timezone)).isoformat(timespec="seconds")
        recent = "\n".join(f"{msg.role}: {msg.content}" for msg in recent_messages)
        tools = self.tool_registry.render_tools_for_prompt()
        system = (
            "你是一个运行在 Discord 中的个人 AI Agent。\n"
            "你可以使用长期记忆、工具、主动推送后台任务和 Drift 空闲任务。\n"
            "当记忆相关时可以使用记忆，但除非用户询问，否则不要解释内部记忆机制。\n\n"
            f"上下文：\n当前时间：{now}\n\n相关记忆：\n{memory_context or '- 无'}\n\n"
            f"最近对话：\n{recent or '- 无'}\n\n"
            f"可用工具：\n{tools or '- 无'}\n"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]
