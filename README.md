# Collie-agent

`Collie-agent` 是一个简化版个人 AI Agent 项目。它保留了个人 Agent 最核心的运行时思想：统一入口、运行时组装、Discord 交互、短期会话、长期记忆、主动推送、Drift 空闲任务和轻量插件系统。

第一版刻意不实现 Telegram、QQ、Web dashboard、多渠道适配、多模型路由、MCP、Docker 部署和复杂权限系统，目标是结构清晰、可运行、可测试。

## 架构概览

## 文档导航

更完整的功能说明和面向技术小白的原理手册在 `docs/` 目录：

- [项目完整说明书](./docs/项目完整说明书.md)
- [记忆系统原理分析](./docs/记忆系统原理分析.md)
- [Proactive 原理分析](./docs/Proactive原理分析.md)
- [Drift 原理分析](./docs/Drift原理分析.md)

```text
Discord
  -> DiscordChannel
  -> MessageBus inbound
  -> AgentLoop
       |- SessionManager
       |- MemoryRuntime
       |- ToolRegistry
       |- EventBus
       |- PluginManager
       `- LLMProvider
  -> MessageBus outbound
  -> DiscordChannel

AppRuntime
  |- AgentLoop
  |- DiscordChannel
  |- ProactiveRuntime
  |    |- ProactiveSourceRegistry
  |    |- ProactiveJudge
  |    `- MemoryRuntime
  `- DriftRuntime
       |- DriftTaskRegistry
       |- MemoryConsolidationTask
       |- RecentContextSummaryTask
       |- ReflectionTask
       |- ProactiveIdeaTask
       `- MemoryDecayTask
```

## 目录结构

```text
Collie-agent/
  main.py
  pyproject.toml
  README.md
  config.example.toml
  bootstrap/
  agent/
  bus/
  channels/
  session/
  memory/
  proactive/
  drift/
  tools/
  plugins/
  plugins_builtin/
  tests/
```

## 安装

项目目标运行环境是 Python 3.12，并使用 `uv` 管理依赖。

```bash
uv sync --dev
```

## 初始化配置

```bash
python main.py init
```

该命令会从 `config.example.toml` 创建 `config.toml`，并创建默认工作区 `./workspace`。

## 配置

默认只需要看根目录的 `config.example.toml`。执行 `python main.py init` 后，它会被复制成 `config.toml`，常用配置已经都在这一份文件里。

推荐流程：

1. 先保持 `llm.provider = "echo"`，它不会联网，也不需要 API key。
2. 填好 Discord 相关环境变量，运行 `test-discord` 检查配置。
3. 需要真实 LLM 时，再把 `llm.provider` 改成 `"openai-compatible"`，并设置 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`。

配置文件支持环境变量占位符：

```toml
bot_token = "${DISCORD_BOT_TOKEN}"
api_key = "${LLM_API_KEY}"
```

常用环境变量：

```powershell
$env:DISCORD_BOT_TOKEN="你的 Discord Bot Token"
$env:DISCORD_GUILD_ID="你的 Discord 服务器 ID"
$env:DISCORD_DEFAULT_PUSH_CHANNEL_ID="默认推送频道 ID"
$env:LLM_MODEL="厂商提供的模型名"
$env:LLM_BASE_URL="厂商提供的 OpenAI 兼容 API 地址"
$env:LLM_API_KEY="你的 LLM API Key"
```

`config.toml` 里的核心字段示例：

```toml
[app]
name = "Collie-agent"
timezone = "America/New_York"

[llm]
provider = "echo"

[llm.compatible]
model = "${LLM_MODEL}"
api_key = "${LLM_API_KEY}"
base_url = "${LLM_BASE_URL}"

[discord]
enabled = true
bot_token = "${DISCORD_BOT_TOKEN}"
guild_id = "${DISCORD_GUILD_ID}"
allowed_channel_ids = []
allowed_user_ids = []
default_push_channel_id = "${DISCORD_DEFAULT_PUSH_CHANNEL_ID}"
```

`configs/` 目录里还保留了拆分版模板，适合想用 `extends` 组合配置的进阶场景。新手可以先忽略它。

不要把真实 Discord bot token 或 API key 写入示例配置，也不要提交到版本库。

## Discord Bot 配置

1. 在 Discord Developer Portal 创建应用。
2. 添加 Bot 用户。
3. 打开 Message Content Intent。
4. 邀请 Bot 到服务器，并授予读取和发送消息权限。
5. 在 `config.toml` 中填写 `discord.bot_token`。
6. 设置 `allowed_user_ids` 和 `allowed_channel_ids`，避免 Bot 响应无关用户或频道。
7. 如需主动推送，设置 `default_push_channel_id`。

如果 `allowed_user_ids` 为空，运行时会输出警告。

## 启动

```bash
python main.py --config config.toml --workspace ./workspace run
```

其他命令：

```bash
python main.py memory
python main.py test-discord
```

## LLM Provider

默认 provider 是 `echo`，不会调用任何外部 API，适合测试和本地开发。

切换到 OpenAI 兼容接口：

```toml
[llm]
provider = "openai-compatible"

[llm.compatible]
model = "厂商提供的模型名"
api_key = "..."
base_url = "厂商提供的 OpenAI 兼容 API 地址"
timeout_seconds = 30
```

OpenAI 兼容 provider 会调用 `/chat/completions`。如果缺少 API key 或请求失败，会抛出清晰的 `LLMError`。

## 记忆系统

长期状态都写入 `workspace/memory/`：

```text
MEMORY.md
PROFILE.md
RECENT_CONTEXT.md
PENDING_MEMORIES.jsonl
MEMORY_INDEX.json
REFLECTIONS.md
CONSOLIDATION_LOG.md
deleted_memories.jsonl
```

文件含义：

- `MEMORY.md`：长期稳定记忆。
- `PROFILE.md`：用户画像、偏好、目标和工作方式。
- `RECENT_CONTEXT.md`：近期上下文摘要。
- `PENDING_MEMORIES.jsonl`：待整理候选记忆。
- `MEMORY_INDEX.json`：结构化记忆索引。
- `REFLECTIONS.md`：Drift 或整理任务生成的阶段性反思。
- `CONSOLIDATION_LOG.md`：每次记忆整理日志。
- `deleted_memories.jsonl`：被软删除记忆的审计记录。

`MemoryRuntime.extract_from_turn()` 会在对话后抽取候选记忆。候选记忆先进入 `PENDING_MEMORIES.jsonl`，再由 `MemoryConsolidator` 去重、合并、处理冲突，并写入 `MEMORY_INDEX.json`、`MEMORY.md` 和 `PROFILE.md`。

搜索不依赖外部向量数据库，会综合关键词、标签、类型、重要性、近期性和置信度打分。

## 主动推送

`ProactiveRuntime` 会定期从 source 中获取候选内容，经 `ProactiveJudge` 打分后，检查 quiet hours、每日推送上限和重复推送记录，再通过 `MessageBus` 发往 Discord。

内置 source：

- `MemoryReminderSource`
- `RecentContextSource`
- `ManualCandidateSource`

手动触发：

```text
!proactive
```

## Drift 空闲任务

`DriftRuntime` 会在用户空闲时运行低优先级后台任务，例如整理记忆、总结近期上下文、写反思、生成主动推送候选和衰减长期未使用记忆。

内置任务：

- `MemoryConsolidationTask`
- `RecentContextSummaryTask`
- `ReflectionTask`
- `ProactiveIdeaTask`
- `MemoryDecayTask`

手动触发：

```text
!drift
```

## Discord 命令

```text
!ask <内容>
!remember <内容>
!memory
!memory search <查询>
!memory 搜索 <查询>
!forget <关键词>
!drift
!proactive
!status
!clear
!help
```

在允许频道中，不以 `!` 开头的消息会被视为普通对话。

## 插件系统

插件保持轻量。每个插件目录提供一个 `plugin.py`，并暴露 `plugin = SomePlugin()` 或 `create_plugin() -> Plugin`。

```python
class MyPlugin:
    name = "my_plugin"

    async def setup(self, context):
        context.tool_registry.register(
            "hello",
            "打招呼。",
            {"type": "object"},
            lambda: "你好",
        )

plugin = MyPlugin()
```

插件可以注册四类扩展：

- event handler
- tool
- proactive source
- drift task

## 新增工具

```python
context.tool_registry.register(
    "my_tool",
    "执行一个有用操作。",
    {"type": "object", "properties": {"value": {"type": "string"}}},
    my_tool,
)
```

工具会通过 `ToolRegistry.render_tools_for_prompt()` 渲染到提示词中。

## 新增主动推送 Source

```python
class MySource:
    name = "my_source"

    async def fetch(self):
        return [
            ProactiveCandidate(
                source=self.name,
                title="提醒",
                content="处理下一步行动。",
            )
        ]

await context.proactive_runtime.add_source(MySource())
```

## 新增 Drift Task

```python
class MyTask:
    name = "my_task"
    interval_seconds = 3600

    async def should_run(self, ctx):
        return True

    async def run(self, ctx):
        return DriftResult(self.name, True, "已完成。")

await context.drift_runtime.add_task(MyTask())
```

## 测试

```bash
uv run pytest
```

如果依赖已经安装，也可以直接运行：

```bash
pytest
```

测试默认使用 `EchoProvider` 和 fake Discord client，不会连接真实 Discord，也不会调用真实 LLM API。
## LLM 分层：main 和 fast

Collie-agent 支持两层 LLM：

- `main` 主模型：用于正式对话、复杂推理、工具调用和最终回复。当前配置中用 `[llm]` 与 `[llm.compatible]` 表示主模型。
- `fast` 轻量模型：用于低成本、低延迟的内部任务。

fast model 适合处理：

- memory gate：判断一轮对话是否需要检索长期记忆。
- query rewrite：把用户原始问题改写成更适合记忆检索的查询。
- HyDE：生成检索增强文本。
- memory extraction：对话后的候选记忆抽取。
- memory consolidation：记忆整理中的分类、去重、冲突和摘要草稿。
- proactive prefilter：主动推送候选的快速预筛。
- drift task scoring：Drift 任务中的轻量分类、评分和摘要草稿。

如果不配置 fast model，系统会自动 fallback 到 main model。

配置示例：

```toml
[llm.fast]
enabled = false
model = ""
api_key = ""
base_url = ""
timeout_seconds = 15
temperature = 0.0
fallback_to_main = true
```

如果要启用独立 fast model：

```toml
[llm.fast]
enabled = true
model = "qwen2.5-7b-instruct"
api_key = "${FAST_LLM_API_KEY}"
base_url = "https://your-fast-provider.example/v1"
timeout_seconds = 15
temperature = 0.0
fallback_to_main = true
```

如果 `fallback_to_main = false`，fast provider 配置不完整或创建失败时会直接报错，适合你希望启动阶段严格检查配置的场景。
