# Agent 6-phase pipeline

本文面向 Collie-agent 维护者，说明当前 AgentLoop 拆分后的执行模型、插件扩展点和工具执行链。这里描述的是运行时结构，不是对外产品说明。

---

## 为什么引入 6-phase

早期 `AgentLoop.process_message()` 同时负责命令处理、意图识别、记忆上下文、prompt 渲染、LLM/tool loop、session 保存、memory extraction、事件发布和 outbound message。这样做直观，但插件很难只插入其中一个稳定位置，后续也难以在不改主循环的情况下增加策略。

6-phase pipeline 的目标是把 turn 内部的稳定边界显式化：

- 主流程仍在 `AgentLoop` 中，用户可见行为保持不变。
- 内置逻辑和插件逻辑都通过 phase module 接入。
- EventBus 事件继续保留，供观测和跨模块联动。
- 工具协议继续使用文本 `<tool_call>{...}</tool_call>`，没有切换到厂商原生 function calling。

当前 phase runner 只按 `priority` 排序，不做 `requires/produces` 拓扑排序。

---

## Phase 职责

执行顺序定义在 `agent/phases.py`：

1. `before_turn`
   - 用户活动记录
   - 命令处理
   - 发布 `BeforeTurnEvent`

2. `before_reasoning`
   - 意图识别
   - 读取 recent session messages
   - 构造 memory context

3. `prompt_render`
   - 调用 `PromptBuilder`
   - 插入 intent system hint
   - 收集 prompt slots
   - 发布 `PromptRenderEvent`

4. `reasoner`
   - 调用 `_complete_with_tools()`
   - 处理文本工具调用协议
   - 通过 `ToolExecutor` 执行工具

5. `after_reasoning`
   - 清理 LLM response，例如 `strip()`
   - 插件可在这里设置跳过持久化或跳过记忆抽取的 slots

6. `after_turn`
   - 保存 session
   - memory extraction
   - 发布 `AfterTurnEvent`
   - publish outbound message
   - trace 收尾

---

## TurnFrame

`TurnFrame` 定义在 `agent/frame.py`，是单轮消息在 phase pipeline 中流动的状态对象。它包含原始入站消息、当前 content、session/channel/user 标识、意图、recent messages、memory context、prompt messages、LLM response、outbound message、trace、slots 和 metadata。

创建方式：

```python
from agent.frame import TurnFrame
from bus.models import InboundMessage

inbound = InboundMessage(
    channel="discord",
    session_id="c1",
    user_id="u1",
    content="  你好  ",
)

frame = TurnFrame.from_inbound(inbound)
assert frame.content == "你好"
assert frame.session_id == "c1"
```

`frame.abort` 表示本轮不再进入后续 reasoning phase。`PhaseRunner` 在某个 module 设置 abort 后，会停止当前 phase 的后续 module。`AgentLoop` 仍会进入 `after_turn`，让 outbound 和 trace 收尾有机会执行。

---

## Slots 约定

`frame.slots` 是轻量的跨 phase 通信机制。插件优先写 slots，而不是直接改 `frame.messages` 或内部 runtime 私有状态。

当前约定：

| Slot | 作用 |
|------|------|
| `prompt:section_top:*` | 按 key 字典序注入到 system prompt 靠前位置，适合高优先级规则 |
| `prompt:section_bottom:*` | 按 key 字典序注入到 system prompt 靠后位置，适合插件提示和上下文提示 |
| `reasoning:max_tool_rounds` | 覆盖本轮 `_complete_with_tools()` 的最大工具轮数 |
| `session:abort_reply` | 设置后短路后续 reasoning，并把该内容作为回复 |
| `memory:skip_extract` | `after_turn` 阶段跳过 memory extraction |
| `session:skip_persist` | `after_turn` 阶段跳过 session history 持久化 |

示例：

```python
class SearchHintModule:
    name = "example.search_hint"
    phase = "prompt_render"
    priority = 20

    async def run(self, frame):
        if "查一下" in frame.content:
            frame.slots["prompt:section_bottom:search_hint"] = (
                "当用户请求查询资料时，优先考虑使用可用搜索工具。"
            )
```

`PromptRenderModule` 会统一收集 `prompt:section_top:*` 和 `prompt:section_bottom:*`，并把值转成字符串后注入 system message。

---

## 插件注册 phase module

插件接口仍然是：

```python
class Plugin:
    name: str

    async def setup(self, context):
        ...
```

插件通过 `context.phase_runner.register(...)` 注册 phase module：

```python
class ExamplePromptPlugin:
    name = "example_prompt_plugin"

    async def setup(self, context):
        context.phase_runner.register(ExamplePromptModule())


class ExamplePromptModule:
    name = "example.prompt_inject"
    phase = "prompt_render"
    priority = 20

    async def run(self, frame):
        frame.slots["prompt:section_bottom:example"] = "插件提供的 prompt 提示。"


plugin = ExamplePromptPlugin()
```

注册失败会沿用 `PluginManager` 的错误处理：默认记录到 `PluginManager.errors` 并继续启动；`strict_plugins = true` 时中断启动。

---

## 插件注册 tool pre-hook

`ToolExecutor` 支持工具调用前 hook。插件可以用它做策略拦截、参数改写、拒绝或要求确认。

```python
from tools.hooks import ToolHookResult


class DangerousToolBlocker:
    name = "policy.dangerous_tool_blocker"
    priority = 10
    tool_name = None

    async def before_tool_call(self, tool_name, arguments, frame):
        if tool_name in {"delete_memory", "send_email", "filesystem_write"}:
            return ToolHookResult(
                decision="confirm",
                reason=f"{tool_name} is a high-risk tool and requires user confirmation.",
            )
        return None


class PolicyPlugin:
    name = "policy_plugin"

    async def setup(self, context):
        context.tool_executor.register_pre_hook(DangerousToolBlocker())


plugin = PolicyPlugin()
```

pre-hook decision：

- `allow`: 继续执行
- `modify`: 用 `ToolHookResult.arguments` 替换本次工具参数
- `deny`: 返回 `{"error": "...", "denied": True}`
- `confirm`: 返回 `{"error": "Tool call requires confirmation.", "requires_confirmation": True, "reason": "..."}`

被 `deny` 或 `confirm` 的工具调用仍会记录 trace，并发布 `ToolCallEvent`。

---

## EventBus 和 phase pipeline 的关系

phase pipeline 是 turn 的主执行结构；EventBus 是运行时事件通知机制。两者不是替代关系。

当前内置 phase module 会继续发布已有事件：

- `BeforeTurnEvent`
- `IntentClassifiedEvent`
- `PromptRenderEvent`
- `BeforeLLMEvent`
- `AfterLLMEvent`
- `ToolCallEvent`
- `BeforeMemoryExtractEvent`
- `AfterMemoryExtractEvent`
- `AfterTurnEvent`

维护建议：

- 需要改变 turn 执行顺序或影响后续 phase，优先写 phase module 或 slots。
- 需要观测、统计、旁路同步，优先订阅 EventBus。
- 不要依赖事件处理器修改主流程状态；事件处理器失败会被 EventBus 记录并吞掉，主流程继续。

---

## ToolRegistry 和 ToolExecutor

`ToolRegistry` 是工具目录：

- 保存工具名、描述、schema、函数和 metadata。
- 维持旧接口 `register(...)` 和 `call_tool(...)`。
- `render_tools_for_prompt()` 当前只渲染 `always_on=True` 的工具，输出文本格式保持兼容。

`ToolExecutor` 是工具执行流程：

- 接收 `ToolRegistry`。
- 支持 `register_pre_hook(...)`。
- 执行 hook chain。
- 最终调用 `ToolRegistry.call_tool(...)`。

AgentLoop 的 tool loop 不直接调用 `tool_registry.call_tool()`，而是调用：

```python
await self.tool_executor.call_tool(call.name, call.arguments, frame=frame)
```

这样可以在不改工具本身的情况下加入策略、确认、审计和参数改写。

---

## 工具调用协议

当前仍然使用文本工具调用协议：

```text
<tool_call>{"name": "calculator", "arguments": {"expression": "1 + 2"}}</tool_call>
```

解析逻辑在 `agent/loop.py` 的 `parse_tool_call()` 中。Collie-agent 目前没有使用 OpenAI、Anthropic 或其他厂商的原生 function calling/tool calling。后续即使接入 MCP 或 deferred tool search，也应保持这个边界清晰：工具目录和执行策略在本地 runtime 中管理，LLM 只通过当前文本协议表达工具调用意图。
