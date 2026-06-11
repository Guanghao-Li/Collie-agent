# Drift 空闲任务

Drift 是 Collie-agent 的后台自维护系统。

它的核心原则是：

```text
用户正在聊天时，不做重活。
用户空闲后，再整理记忆、压缩上下文、反思、生成主动候选。
```

这样可以把低实时性工作移出用户请求路径，避免用户发消息时被后台整理拖慢。

---

## 开启配置

```toml
[drift]
enabled = true
interval_seconds = 1800
run_only_when_idle = true
idle_after_seconds = 600
max_tasks_per_cycle = 2
```

字段含义：

| 字段 | 说明 |
|------|------|
| `enabled` | 是否启动 DriftRuntime |
| `interval_seconds` | Drift tick 间隔 |
| `run_only_when_idle` | 是否只在用户空闲时运行 |
| `idle_after_seconds` | 距离上次用户活动多久算空闲 |
| `max_tasks_per_cycle` | 每轮最多执行几个 Drift task |

---

## Drift 每轮怎么判断

```text
DriftRuntime.start()
  ↓
每 interval_seconds 秒醒来
  ↓
检查 drift.enabled
  ↓
读取 last_user_activity_at
  ↓
如果 run_only_when_idle = true：
      当前时间 - last_user_activity_at < idle_after_seconds
          → 跳过
      当前时间 - last_user_activity_at >= idle_after_seconds
          → 进入任务选择
  ↓
从 DriftTaskRegistry 选择可运行任务
  ↓
最多执行 max_tasks_per_cycle 个
  ↓
发布任务前后事件
```

---

## 为什么需要 idle 检测

记忆整理、上下文压缩、反思和候选生成通常不需要在用户发消息时立即完成。

如果把这些任务塞进每轮对话里，会带来几个问题：

- 回复延迟变高
- LLM 成本上升
- 文件写入频率过高
- 用户正在聊天时，后台状态可能频繁变化
- 主循环职责变复杂

Drift 把这些工作放到空闲期做，让被动回复链路保持清晰：

```text
用户消息路径：
Discord → MessageBus → AgentLoop → LLM → Discord

空闲维护路径：
DriftRuntime → DriftTaskRegistry → Memory / Proactive / EventBus
```

---

## 内置任务类型

### 1. memory_consolidation

把 `PENDING_MEMORIES.jsonl` 中的候选整理到 Markdown 缓冲层。

```text
PENDING_MEMORIES.jsonl
  ↓
memory_consolidation
  ├── HISTORY.md
  ├── PENDING.md
  └── RECENT_CONTEXT.md
```

这个任务负责把“原始候选”变成“可读、可继续处理的记忆材料”。

---

### 2. recent_context_summary

压缩近期 session，更新 `RECENT_CONTEXT.md`。

它记录：

- 最近持续关注的话题
- 正在推进的问题
- 未完成的上下文
- 用户近期偏好或限制
- 下次对话应该延续的线索

`RECENT_CONTEXT.md` 会在回复前进入记忆上下文，帮助 Agent 不需要完整历史也能知道最近在聊什么。

---

### 3. reflection

写入轻量后台反思。

它可以总结：

- 最近对话里有哪些服务质量问题
- 用户最近反复强调了什么
- 哪些记忆可能需要修正
- 哪些主动推送规则需要更保守
- 哪些上下文下不应该打扰用户

reflection 不应该直接替代长期记忆。它更像运行时自检。

---

### 4. proactive_idea

基于已有记忆和近期上下文生成主动跟进候选。

例如：

```text
用户之前提到某个项目 deadline
  ↓
Drift 在空闲时发现这个事项可能需要跟进
  ↓
生成 proactive candidate
  ↓
ProactiveRuntime 决定是否推送
```

重点：Drift 只生成候选，不直接推送。

最终是否发给用户，要经过 ProactiveRuntime 的 prefilter、judge、quiet hours、daily cap 和去重。

---

### 5. memory_decay

对长期未使用、低置信度或过期记忆做衰减。

可能动作：

- 降低权重
- 标记 review
- 从 active memory 中移出
- 保留到 history 但不再高优先级注入

这个任务用于避免长期记忆越来越臃肿。

---

## Drift 和 Memory 的关系

Drift 是 memory 生命周期里的后台执行者。

```text
对话结束后：
  MemoryExtractor 抽取候选
  ↓
  PENDING_MEMORIES.jsonl

用户空闲后：
  DriftRuntime 执行 memory_consolidation
  ↓
  HISTORY.md / PENDING.md / RECENT_CONTEXT.md

低频优化：
  MemoryOptimizer 处理 PENDING.md
  ↓
  MEMORY.md / SELF.md / MEMORY_INDEX.json
```

没有 Drift，记忆仍然可以有基础写入能力，但系统会少掉空闲期整理、近期上下文压缩和主动候选生成这类后台维护能力。

---

## Drift 和 Proactive 的关系

```text
DriftRuntime
  ↓
空闲期生成候选
  ↓
ProactiveRuntime
  ↓
判断是否值得推
  ↓
Discord
```

职责划分：

| 模块 | 职责 |
|------|------|
| Drift | 后台整理、思考、生成候选 |
| Proactive | 判断候选是否值得现在推送 |
| DiscordChannel | 真正把消息发出去 |

这样设计可以避免 Drift task 自己绕过静默时间和每日额度。

---

## 插件如何扩展 Drift

插件可以注册新的 Drift task。

适合做成 Drift task 的事情：

- 定期整理某类外部数据
- 分析最近对话质量
- 生成某类主动候选
- 清理过期缓存
- 做低频索引重建
- 做轻量自检

不适合做成 Drift task 的事情：

- 必须立即响应用户的操作
- 高风险文件修改
- 无限循环或长时间阻塞任务
- 需要用户明确确认的动作
- 会绕过主动推送约束直接打扰用户的逻辑

伪代码：

```python
class MyDriftTask:
    name = "my_drift_task"

    async def should_run(self, context):
        return True

    async def run(self, context):
        # 做低频后台工作
        return {"ok": True}
```

插件注册：

```python
def setup(self, context):
    context.drift_runtime.tasks.register(MyDriftTask())
```

实际接口以当前 `DriftTaskRegistry` 代码为准。文档中的伪代码只表达任务职责。

---

## 调试 Drift

常见问题：

| 现象 | 检查 |
|------|------|
| Drift 完全不运行 | `[drift].enabled` |
| Drift 一直跳过 | `run_only_when_idle`、`idle_after_seconds`、last user activity |
| 任务太多 | 降低 `max_tasks_per_cycle` 或加 task 级别冷却 |
| 记忆没整理 | memory_consolidation 是否注册、pending 是否存在 |
| 主动候选没出现 | proactive_idea 是否运行、ProactiveRuntime 是否启用 |
| 用户聊天时后台抢资源 | 确认 `run_only_when_idle = true` |

---

## 当前边界

- Drift 是单进程后台循环，不是分布式任务队列。
- task 执行时间过长会影响后续 tick。
- 多实例部署需要外部锁，避免多个进程同时整理同一份 memory 文件。
- 插件 task 需要自己处理异常边界，避免一次失败污染长期状态。
