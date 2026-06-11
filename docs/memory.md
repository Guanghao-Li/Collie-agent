# 记忆系统

Collie-agent 的记忆系统分为两层：

1. **Markdown 文件层**：人类可读，方便审计、迁移、手动修正。
2. **结构化 / 向量层**：`MEMORY_INDEX.json` 和可选 `memory2.db`，用于检索、去重、增强召回。

重点不是“把所有历史聊天塞进 prompt”，而是把对话变成可维护的长期状态。

---

## 文件布局

默认记忆目录：

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

| 文件 | 写入方 | 读取方 | 用途 |
|------|--------|--------|------|
| `SELF.md` | `MemoryOptimizer` | `MemoryRuntime` / prompt | 用户画像、稳定偏好、服务规则 |
| `MEMORY.md` | `MemoryOptimizer` | `MemoryRuntime` / prompt | 稳定长期记忆 |
| `HISTORY.md` | `MemoryConsolidator` | 检索 / dashboard / 后续整理 | 按时间追加的事件摘要 |
| `RECENT_CONTEXT.md` | `MemoryConsolidator` / Drift task | `MemoryRuntime` / prompt | 近期压缩上下文 |
| `PENDING.md` | `MemoryConsolidator` | `MemoryOptimizer` | 待优化长期记忆候选 |
| `MEMORY_INDEX.json` | `MemoryOptimizer` | 检索 / dashboard | 结构化索引 |
| `PENDING_MEMORIES.jsonl` | `MemoryExtractor` | `MemoryConsolidator` | 每轮对话后抽取出的原始候选 |

---

## 为什么不是直接写 MEMORY.md？

因为对话里出现的一句话不一定是长期事实。

例如：

- 用户临时说“今天想吃披萨”，不一定代表长期偏好。
- 用户纠正旧信息时，需要替换已有记忆，而不是简单追加。
- 健康、法律、财务、安全类信息需要更谨慎。
- 模型可能误抽取，需要 pending 层承接不确定性。

所以 Collie-agent 把记忆写入分成四步：

```text
对话结束
  ↓
MemoryExtractor 抽取候选
  ↓
PENDING_MEMORIES.jsonl
  ↓
MemoryConsolidator 整理到 Markdown 缓冲层
  ├── HISTORY.md
  ├── PENDING.md
  └── RECENT_CONTEXT.md
  ↓
MemoryOptimizer 低频优化
  ├── MEMORY.md
  ├── SELF.md
  └── MEMORY_INDEX.json
```

`MEMORY.md` 和 `SELF.md` 是稳定层，不应该被每轮对话频繁修改。

---

## 读取路径：回复前如何使用记忆

每次普通消息进入 `AgentLoop` 后，会调用：

```text
MemoryRuntime.build_memory_context()
```

它会读取三类信息：

```text
1. SELF.md
   ↓
   用户画像、偏好、长期服务规则

2. RECENT_CONTEXT.md
   ↓
   最近在聊什么、哪些话题需要延续

3. 检索结果
   ↓
   和当前消息相关的长期记忆、历史事件或结构化索引结果
```

最终这些内容进入 prompt，帮助模型在回复时保持上下文连续性。

---

## Memory gate：不是每轮都重搜

检索前会先做 memory gate。

有 fast model 时：

```text
用户当前消息
  ↓
fast model 判断：
  - 是否需要搜索长期记忆
  - 应该搜什么 query
  - 更像哪类 memory type
  ↓
需要搜索才进入 retriever
```

没有 fast model 或使用 `echo` provider 时，系统退回规则判断。

这样可以避免每条闲聊都做昂贵检索，也能在真正需要记忆时更准确地组织 query。

---

## Query rewrite 和 HyDE

启用 `enable_hyde = true` 后，系统会让 fast model 生成一个“假想记忆片段”。

```text
用户问：
“上次我说那个项目 deadline 是哪天？”

fast model 生成：
“用户之前提到某项目的截止日期是……”

然后用：
  - 原始 query
  - 改写 query
  - 假想记忆片段
一起增强检索
```

这对模糊问题有帮助，因为用户经常不会复述完整关键词。

---

## 写入路径：一轮对话怎么变成记忆

### 1. 抽取候选

对话结束后，`MemoryExtractor` 从本轮用户消息和助手回复里抽取候选。

候选先进入：

```text
PENDING_MEMORIES.jsonl
```

这一步不直接改稳定记忆。

### 2. Consolidation

`MemoryConsolidator` 会把 pending 队列整理到 Markdown 缓冲层：

```text
PENDING_MEMORIES.jsonl
  ↓
MemoryConsolidator
  ├── HISTORY.md
  ├── PENDING.md
  └── RECENT_CONTEXT.md
```

典型分流：

- 事件、阶段性进展、当天发生的事 → `HISTORY.md`
- 稳定偏好、身份、长期信息、明确要求记住的事 → `PENDING.md`
- 最近上下文摘要、持续话题、未完事项 → `RECENT_CONTEXT.md`

### 3. Optimizer

`MemoryOptimizer` 低频处理 `PENDING.md`。

它会做：

- 精确重复合并
- reinforcement 计数
- 新事实归档
- correction 替换旧事实
- supersede 旧条目
- 敏感项或冲突项标记 review
- 重新渲染 `MEMORY.md`、`SELF.md`、`MEMORY_INDEX.json`

```text
PENDING.md
  ↓
MemoryOptimizer
  ↓
MEMORY.md + SELF.md + MEMORY_INDEX.json
```

---

## 配置

默认配置适合先跑通 Markdown + keyword 检索：

```toml
[memory]
enabled = true
auto_extract = true
auto_consolidate = true
optimizer_enabled = true
optimizer_auto_run = false
optimizer_interval_seconds = 64800
optimizer_min_pending = 1

enable_hyde = true
enable_vector_memory = false

search_limit = 8
memory_injection_budget_chars = 3500
workspace_dir = "memory"
```

### 启用向量记忆

向量记忆默认关闭。打开后，需要 embedding 配置：

```toml
[memory]
enable_vector_memory = true
vector_db_path = ".collie/memory/memory2.db"
vector_top_k = 12
vector_score_threshold = 0.72

[memory.embedding]
model = "text-embedding-v3"
api_key = "${DASHSCOPE_API_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
timeout_seconds = 20
```

如果 embedding 配置缺失，系统会记录 disabled reason，并降级到 keyword-only。它不会因为向量记忆失败而阻断主程序启动。

---

## Dashboard

启用本地 memory dashboard：

```toml
[memory.server]
enabled = true
host = "127.0.0.1"
port = 8765
api_key = "change-me"
```

打开：

```text
http://127.0.0.1:8765/dashboard
```

主要用途：

- 查看 memory stats
- 搜索记忆
- 查看单条 memory
- 手动 memorize
- 手动 recall
- 更新或删除记忆
- 触发 optimize
- 查找相似记忆
- 批量删除

默认建议只绑定 `127.0.0.1`。如果改成公网地址，必须额外配置鉴权、TLS 和反向代理安全边界。

---

## 手动检查建议

调试记忆时，优先看这些文件：

```text
workspace/memory/PENDING_MEMORIES.jsonl
workspace/memory/PENDING.md
workspace/memory/MEMORY.md
workspace/memory/SELF.md
workspace/memory/RECENT_CONTEXT.md
workspace/memory/MEMORY_INDEX.json
```

常见判断：

| 现象 | 看哪里 |
|------|--------|
| 对话后没有候选 | `PENDING_MEMORIES.jsonl` |
| 候选没有整理进 Markdown | `PENDING.md` / consolidation 日志 |
| 长期记忆没更新 | `MemoryOptimizer` 配置和 `optimizer_state_path` |
| 回复没有带上记忆 | `build_memory_context()` 输出和检索设置 |
| 向量检索没生效 | embedding 配置、`enable_vector_memory`、disabled reason |
