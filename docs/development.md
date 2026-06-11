# Collie-agent 开发与运维文档

这份文档记录本地启动、配置、常用命令、扩展方式、测试和排障建议。

## 1. 本地环境

要求：

- Python 3.12+
- `uv` 或等价 Python 虚拟环境工具
- Windows、macOS、Linux 均可运行

安装依赖：

```bash
uv sync --dev
```

如果只使用默认 `echo` provider，不需要外部 API key。如果要接真实模型，把 `config.toml` 中 `[llm] provider` 改为 `openai-compatible`，并配置模型名、base URL 和 API key。

## 2. 初始化

```bash
python main.py init
```

这个命令会：

- 如果不存在 `config.toml`，从 `config.example.toml` 复制一份。
- 创建默认 workspace 目录。

推荐把敏感值放进 `.env`：

```bash
DISCORD_BOT_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_DEFAULT_PUSH_CHANNEL_ID=...
LLM_MODEL=...
LLM_API_KEY=...
LLM_BASE_URL=...
```

TOML 里可以用 `${ENV_NAME}` 引用环境变量。

## 3. 常用命令

检查 Discord 配置：

```bash
python main.py --config config.toml --workspace ./workspace test-discord
```

运行 Agent：

```bash
python main.py --config config.toml --workspace ./workspace run
```

打印记忆文件：

```bash
python main.py --config config.toml --workspace ./workspace memory
```

运行测试：

```bash
pytest
```

只跑某个测试文件：

```bash
pytest tests/test_memory_runtime.py
```

## 4. 配置建议

### 4.1 LLM

本地验证时保持：

```toml
[llm]
provider = "echo"
```

接真实模型时：

```toml
[llm]
provider = "openai-compatible"

[llm.compatible]
model = "${LLM_MODEL}"
api_key = "${LLM_API_KEY}"
base_url = "${LLM_BASE_URL}"
```

如果有便宜、低延迟的小模型，可以启用 fast model：

```toml
[llm.fast]
enabled = true
model = "${FAST_LLM_MODEL}"
api_key = "${FAST_LLM_API_KEY}"
base_url = "${FAST_LLM_BASE_URL}"
temperature = 0.0
fallback_to_main = true
```

fast model 会用于记忆搜索判断、query 改写、HyDE、主动推送预筛选等内部任务。

### 4.2 Discord

建议长期使用时限制频道和用户：

```toml
[discord]
allowed_channel_ids = ["123456789012345678"]
allowed_user_ids = ["123456789012345678"]
default_push_channel_id = "123456789012345678"
```

`default_push_channel_id` 用于主动推送。如果不启用主动推送，可以先留空。

### 4.3 Memory

默认记忆是 Markdown + keyword 检索，适合先跑通：

```toml
[memory]
enabled = true
enable_vector_memory = false
search_limit = 8
memory_injection_budget_chars = 3500
```

启用向量记忆时需要 embedding 配置：

```toml
[memory]
enable_vector_memory = true

[memory.embedding]
model = "text-embedding-v3"
api_key = "${DASHSCOPE_API_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
```

配置缺失时，系统会记录 disabled reason 并降级，不会因为向量记忆失败而阻断启动。

### 4.4 Memory Server

启用 dashboard：

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

如果设置了 API key，dashboard 页面需要输入 key，后续请求会带上鉴权头。

## 5. 开发工具

新增工具的推荐流程：

1. 在 `tools/` 中实现函数。
2. 在 `bootstrap/tools.py` 或插件中注册到 `ToolRegistry`。
3. 提供清晰描述和参数 schema。
4. 添加测试覆盖正常路径、错误路径和参数边界。

工具函数可以是同步或异步函数。抛出的普通异常会被包装成 `ToolError`，返回给模型作为工具结果。

工具 schema 示例：

```python
registry.register(
    name="calculator",
    description="计算一个安全的数学表达式",
    schema={"type": "object", "properties": {"expression": {"type": "string"}}},
    func=calculate,
)
```

## 6. 开发插件

插件目录结构：

```text
my_plugins/
  my_plugin/
    plugin.py
```

`plugin.py` 暴露 `plugin` 或 `create_plugin()`。插件可以在 `setup()` 中注册工具、监听事件、增加 proactive source 或 Drift task。

配置插件路径：

```toml
[plugins]
enabled = true
paths = ["plugins_builtin", "my_plugins"]
strict_plugins = false
```

开发建议：

- 插件失败默认不应拖垮主进程，除非确实需要 `strict_plugins = true`。
- 插件不要直接改内部私有属性，优先通过 context 暴露的 runtime 和 registry 交互。
- 涉及外部网络、文件写入或危险工具时，需要自己做权限边界和输入校验。

## 7. 开发新的 Channel

Channel 的职责是把外部系统适配成内部消息：

- 外部输入转换为 `InboundMessage` 并发布到 `MessageBus`。
- 从 `MessageBus` 消费 `OutboundMessage` 并发送到外部系统。
- 在 start/stop 中管理连接生命周期。

Discord channel 可以作为参考。新增 channel 时，尽量不要把业务逻辑写进 channel；业务逻辑应留在 AgentLoop、MemoryRuntime 或插件中。

## 8. 测试策略

当前测试重点覆盖：

- 配置加载
- Agent loop smoke
- MessageBus/EventBus
- SessionManager
- ToolRegistry
- PluginManager
- Discord channel 基础行为
- Memory runtime、retriever、server、scheduler、optimizer、consolidation
- ProactiveRuntime 和 DriftRuntime

文档改动通常不需要跑全量测试。代码改动建议至少跑：

```bash
pytest
```

如果只改 memory：

```bash
pytest tests/test_memory_runtime.py tests/test_memory_retriever.py tests/test_memory_optimizer_stage3b.py
```

如果只改插件或工具：

```bash
pytest tests/test_plugin_manager.py tests/test_tool_registry.py
```

## 9. 排障

### 9.1 Discord 已启用但没有回复

检查：

- `discord.bot_token` 是否为空。
- Bot 是否被加入 guild。
- `allowed_channel_ids` 是否包含当前频道。
- `allowed_user_ids` 是否包含当前用户。
- Bot 是否有读取消息和发送消息权限。

### 9.2 LLM 请求失败

检查：

- `llm.provider` 是否为 `openai-compatible`。
- `model`、`api_key`、`base_url` 是否都不为空。
- base URL 是否以服务商文档为准。
- 网络是否能访问该服务。

### 9.3 记忆没有进入长期记忆

这是预期的分阶段行为。候选通常先进入 pending，再由 Drift 或手动命令触发整理和优化。

检查：

- `[memory] enabled` 和 `auto_extract` 是否开启。
- `PENDING_MEMORIES.jsonl` 是否有候选。
- `PENDING.md` 是否有待优化候选。
- `optimizer_enabled` 是否开启。
- 候选是否因为 correction、敏感内容或冲突被标为 review。

### 9.4 向量记忆没有生效

检查：

- `enable_vector_memory = true`
- embedding model、API key、base URL 是否完整。
- `sqlite-vec` 是否安装。
- dashboard 或 `describe()` 中的 disabled reason。

默认降级是设计行为：向量能力缺失不应阻断 Agent 主流程。

### 9.5 主动推送太少

检查：

- `[proactive] enabled`
- 当前时间是否在 quiet hours。
- `max_pushes_per_day` 是否已达到。
- `min_score_to_push` 是否过高。
- 是否有 source 产生候选。
- fast prefilter 是否过滤过严。

### 9.6 Drift 不运行

检查：

- `[drift] enabled`
- `run_only_when_idle`
- `idle_after_seconds`
- 用户最近是否刚发送过消息。
- `max_tasks_per_cycle` 是否过小。

## 10. 发布前检查

提交代码前建议确认：

- 没有提交 `.env`、`config.toml`、`workspace/` 或 `private_docs/`。
- README 和 docs 中的命令仍能对应当前入口。
- 新配置在 `config.example.toml` 和 `bootstrap/config.py` 中一致。
- 新能力默认可关闭。
- 涉及记忆、工具或插件的改动有测试覆盖。
