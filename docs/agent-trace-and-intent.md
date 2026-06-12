# Agent Trace 与 Intent 路由提示

## Trace 机制

Trace 用来记录每一轮非显式命令消息在 AgentLoop 中的运行过程，重点帮助调试 ReAct-like tool loop：LLM 调用、工具调用、工具 observation、延迟、意图识别结果、记忆提取数量和结束原因。

显式 `!` 命令仍由 `agent/commands.py` 优先处理；命令直接返回时不会强制生成完整 trace。普通消息进入 `AgentLoop.process_message()` 后会创建 turn-level trace，并在 `_complete_with_tools()` 中记录每轮 LLM step 和 tool step。

默认输出路径：

```text
<workspace>/traces/agent_traces.jsonl
```

每行是一个 JSON 对象，主要字段包括：

- `trace_id`
- `session_id`
- `started_at` / `finished_at` / `duration_ms`
- `user_message_preview`
- `intent`
- `memory_context_chars`
- `prompt_message_count`
- `steps`
- `finish_reason`
- `memory_extracted_count`
- `error`

`steps` 中的 `llm` step 会记录轮次、purpose、延迟、输出预览、是否产生工具调用和工具名。`tool` step 会记录轮次、工具名、参数、结果预览、延迟和错误摘要。

Trace 不记录 hidden chain-of-thought，也不默认保存完整 prompt。用户消息、LLM 输出和工具结果只保存有限长度 preview，默认最大 500 字符。工具参数和结果会先转成 JSON-safe 结构。trace 写入失败只记日志，不影响用户回复流程。

## Dashboard Trace Viewer

Memory Dashboard 在 trace recording 开启时可以查看最近的 agent trace。Dashboard 通过 memory server 的只读 trace API 读取 JSONL 文件，展示最近轮次、finish reason、duration、intent 决策以及 LLM/tool steps。这个 viewer 面向本地调试和 demo，不是完整 observability 平台。

## Intent 路由提示

`IntentRouter` 的目标是在显式命令未命中后，给 AgentLoop 一个轻量 routing hint。它不会直接执行动作，也不会替代原有 `!` 命令系统；最终回复仍由 Agent 根据用户原始消息和上下文判断。

处理顺序：

1. `agent/commands.py` 先处理显式命令。
2. 非命令消息进入 `IntentRouter.classify()`。
3. 规则优先分类。
4. 低置信度且配置允许时，使用 fast LLM fallback。
5. fallback 返回非法 JSON 或非法 intent 时，安全降级为 `general_chat`。
6. 发布 `IntentClassifiedEvent`，并把 intent 作为短 system hint 注入 prompt。

当前支持的 intent：

- `general_chat`
- `tool_execution`
- `memory_add`
- `memory_correction`
- `memory_delete`
- `proactive_config`
- `drift_task_create`
- `dashboard_command`
- `plugin_management`

## 配置

可在 `config.toml` 中配置：

```toml
[trace]
enabled = true
path = "traces/agent_traces.jsonl"
max_preview_chars = 500

[intent]
enabled = true
llm_fallback_enabled = true
fallback_confidence_threshold = 0.55
timeout_seconds = 5
```

## 扩展新的 intent

1. 在 `agent/intent.py` 的 `SUPPORTED_INTENTS` 增加 intent 名称。
2. 在 `ROUTES` 中增加对应 route。
3. 在 `_classify_by_rules()` 中加入中文和英文常见表达规则。
4. 如需 fallback 支持，确保 LLM prompt 的 allowed intents 包含新名称。
5. 在 `tests/test_intent.py` 增加规则和 fallback 测试。

## 验证

建议先运行新增测试：

```bash
pytest tests/test_intent.py
pytest tests/test_trace.py
pytest tests/test_agent_loop_trace.py
```

最后运行完整测试：

```bash
pytest
```
