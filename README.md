# Collie-agent

一个**会长期记得你、会在 Discord 主动找你**的个人 AI Agent Runtime。

Collie-agent 不是一次性的聊天机器人。它会把你的对话沉淀成长期记忆，在下一次聊天时检索相关背景；也会在你不活跃时做后台整理，把零散上下文压缩、归档、优化；当系统判断某条提醒或跟进建议确实有价值时，它可以主动推送给你。

---

## Quickstart

需要 Python 3.12+。

```bash
git clone <this-repo>
cd Collie-agent
uv sync --dev
```

**1. 初始化**

```bash
python main.py init
```

这个命令会复制 `config.example.toml` 到 `config.toml`，并创建默认 workspace。

**2. 先用 echo provider 跑通主链路**

默认配置使用 `echo`，不联网、不需要模型 API key，适合先验证 Discord、消息总线、AgentLoop、记忆文件和后台任务能不能启动。

```bash
python main.py --config config.toml --workspace ./workspace test-discord
python main.py --config config.toml --workspace ./workspace run
```

**3. 接入真实模型**

把 `[llm] provider` 改成 `openai-compatible`，并配置模型名、API key 和 base URL。

```toml
[llm]
provider = "openai-compatible"

[llm.compatible]
model = "${LLM_MODEL}"
api_key = "${LLM_API_KEY}"
base_url = "${LLM_BASE_URL}"
timeout_seconds = 30
temperature = 0.7

[llm.fast]
enabled = true
model = "${FAST_LLM_MODEL}"
api_key = "${FAST_LLM_API_KEY}"
base_url = "${FAST_LLM_BASE_URL}"
temperature = 0.0
fallback_to_main = true
```

fast model 用来跑低成本内部任务：记忆搜索判断、query rewrite、HyDE、主动推送预筛选、Drift 摘要等。

**4. 配置 Discord**

```toml
[discord]
enabled = true
bot_token = "${DISCORD_BOT_TOKEN}"
guild_id = "${DISCORD_GUILD_ID}"

# 长期使用时建议限制频道和用户
allowed_channel_ids = ["123456789012345678"]
allowed_user_ids = ["123456789012345678"]

# 主动推送默认发送到这里
default_push_channel_id = "${DISCORD_DEFAULT_PUSH_CHANNEL_ID}"
```

敏感信息建议放在 `.env` 中，通过 `${ENV_NAME}` 在 TOML 里引用。`.env`、`config.toml`、`workspace/` 默认不应该提交。

---

## 系统全景

```text
你的 Discord 消息
    ↓
[DiscordChannel] → [MessageBus] → [AgentLoop]
                                      │
                                      ├── SessionManager：最近对话
                                      ├── MemoryRuntime：长期记忆检索 / 注入 / 抽取
                                      ├── PromptBuilder：拼 system prompt、工具 schema、上下文
                                      ├── LLMProvider：主模型或 echo provider
                                      └── ToolRegistry：工具调用

后台循环
    ├── [ProactiveRuntime]
    │       候选来源 → fast prefilter → judge 打分 → 静默时间 / 每日额度 / 去重 → Discord 推送
    │
    └── [DriftRuntime]
            用户空闲检测 → 记忆整理 / 近期上下文摘要 / 反思 / 主动候选 / 记忆衰减
```

| 想看什么 | 文档 |
|---------|------|
| MEMORY.md / SELF.md / HISTORY.md / PENDING.md / 记忆怎么流转 | [docs/memory.md](./docs/memory.md) |
| 主动推送怎么筛候选、怎么打分、怎么控频 | [docs/proactive.md](./docs/proactive.md) |
| Drift 空闲任务在做什么、什么时候运行、怎么扩展 | [docs/drift.md](./docs/drift.md) |
| 怎么写插件注册工具、主动源、Drift task | [docs/plugins.md](./docs/plugins.md) |
| 系统边界、消息流、运行时装配 | [docs/architecture.md](./docs/architecture.md) |
| 本地启动、配置、测试、排障 | [docs/development.md](./docs/development.md) |

---

## 被动回复

收到用户消息后，Collie-agent 不是直接把消息丢给模型。它会先走一条完整的运行时链路：

```text
Discord 消息
  → InboundMessage
  → AgentLoop
  → 命令检查
  → 读取近期 session
  → MemoryRuntime.build_memory_context()
  → PromptBuilder 注入时间、记忆、最近对话、工具列表
  → LLMProvider 生成回复或 tool_call
  → ToolRegistry 执行工具
  → 最终回复发回 Discord
  → 对话结束后触发记忆抽取
```

工具调用目前使用文本协议：

```text
<tool_call>
{"name": "calculator", "arguments": {"expression": "1 + 1"}}
</tool_call>
```

这个设计简单、可测、对 echo provider 友好；边界是它依赖模型按格式输出，不是厂商原生 function calling。

---

## 主动推送

Collie-agent 可以主动推送，但不是“有候选就发”。

每轮 proactive tick 会先从注册的 source 里拿候选，再经过 fast prefilter、judge、quiet hours、daily cap 和去重。只有候选足够相关、当前时间适合打扰、当天额度没用完时，才会发到 Discord。

```text
ProactiveSourceRegistry
  ↓
候选来源
  ├── 记忆提醒
  ├── 近期上下文
  ├── Drift 生成的主动跟进候选
  ├── 手动候选
  └── 插件注册的新 source
  ↓
fast prefilter
  ↓
judge 打分
  ↓
quiet hours 检查
  ↓
max_pushes_per_day 检查
  ↓
candidate id 去重
  ↓
MessageBus → Discord
```

详见 [docs/proactive.md](./docs/proactive.md)。

---

## 记忆系统

Collie-agent 的记忆系统重点不是“把所有话都塞进 prompt”，而是分层处理。

```text
每轮对话
  ↓
MemoryExtractor 抽取候选
  ↓
PENDING_MEMORIES.jsonl
  ↓
Drift 空闲期 consolidation
  ├── HISTORY.md          时间线事件
  ├── PENDING.md          待优化长期记忆候选
  └── RECENT_CONTEXT.md   近期上下文压缩摘要
  ↓
MemoryOptimizer 低频优化
  ├── MEMORY.md           稳定长期记忆
  ├── SELF.md             用户画像、偏好、服务规则
  ├── MEMORY_INDEX.json   结构化索引
  └── memory2.db          可选 SQLite / 向量层
```

默认记忆文件在：

```text
workspace/memory/
  SELF.md
  MEMORY.md
  HISTORY.md
  RECENT_CONTEXT.md
  PENDING.md
  MEMORY_INDEX.json
  PENDING_MEMORIES.jsonl
```

详见 [docs/memory.md](./docs/memory.md)。

---

## Drift 空闲任务

Drift 是 Collie-agent 的后台自维护系统。

用户正在聊天时，AgentLoop 优先保证低延迟回复；用户不活跃后，DriftRuntime 才开始做更重、低实时性的工作：整理 pending 记忆、压缩近期上下文、写反思、生成主动推送候选、做记忆衰减。

```toml
[drift]
enabled = true
interval_seconds = 1800
run_only_when_idle = true
idle_after_seconds = 600
max_tasks_per_cycle = 2
```

详见 [docs/drift.md](./docs/drift.md)。

---

## 插件系统

Collie-agent 的扩展点不要求改 AgentLoop。

插件可以：

- 注册工具到 `ToolRegistry`
- 监听运行时事件
- 增加 proactive source
- 增加 Drift task
- 访问 memory runtime、message bus、LLM provider 等上下文

插件目录结构：

```text
my_plugins/
  my_plugin/
    plugin.py
```

`plugin.py` 暴露 `plugin` 或 `create_plugin()`。

配置：

```toml
[plugins]
enabled = true
paths = ["plugins_builtin", "my_plugins"]
strict_plugins = false
```

详见 [docs/plugins.md](./docs/plugins.md)。

---

## Memory Dashboard

Collie-agent 内置可选 memory dashboard / API，用来查看、搜索、更新和删除记忆。

```toml
[memory.server]
enabled = true
host = "127.0.0.1"
port = 8765
api_key = "change-me"
```

启动后打开：

```text
http://127.0.0.1:8765/dashboard
```

主要能力：

- 查看 memory stats
- 搜索长期记忆
- 手动 memorize / recall
- 更新或删除记忆
- 触发 optimize
- 查找相似记忆
- 批量删除

默认建议只绑定 `127.0.0.1`。如果暴露到公网，必须配置鉴权和反向代理安全边界。

---

## 常用命令

```bash
python main.py init

python main.py --config config.toml --workspace ./workspace test-discord

python main.py --config config.toml --workspace ./workspace run

python main.py --config config.toml --workspace ./workspace memory

pytest
```

只改 memory 时，建议至少跑：

```bash
pytest tests/test_memory_runtime.py tests/test_memory_retriever.py tests/test_memory_optimizer_stage3b.py
```

只改插件或工具时：

```bash
pytest tests/test_plugin_manager.py tests/test_tool_registry.py
```

---

## 项目结构

```text
agent/            Agent 主循环、LLM provider、prompt 和内置命令
bootstrap/        配置加载、运行时装配、provider/tool/plugin/background 初始化
bus/              inbound/outbound message bus 和运行时事件总线
channels/         Discord 等外部消息通道
drift/            用户空闲期后台任务
memory/           记忆运行时、Markdown store、检索、优化、dashboard/API
plugins/          插件协议、上下文和加载器
plugins_builtin/  内置 memory/proactive/drift 插件
proactive/        主动推送来源、判断和运行时
session/          会话历史管理
tools/            工具注册表和内置工具
tests/            pytest 测试
docs/             技术文档
```

---

## 当前边界

- 默认是单进程运行时，不包含分布式队列、分布式锁和多实例一致性。
- Discord 是默认 channel，其他 IM 或 Web channel 需要新增适配器。
- 工具调用协议目前是文本约定，不是厂商原生 function calling。
- 向量记忆是可选增强，不是默认依赖。
- 主动推送依赖候选质量、记忆质量和规则阈值，长期使用需要继续调参和观察。
- memory dashboard 默认适合本地管理；公网部署需要额外鉴权和网络安全设计。
