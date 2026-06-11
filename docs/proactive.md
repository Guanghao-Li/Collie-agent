# 主动推送

Collie-agent 的主动推送不是简单的定时消息，也不是“有内容就发”。

它是一条带约束的候选评估链路：

```text
ProactiveSourceRegistry
  ↓
收集候选
  ↓
fast prefilter
  ↓
judge 打分
  ↓
quiet hours / daily cap / dedup
  ↓
MessageBus
  ↓
DiscordChannel
```

目标是让 Agent **有理由地打扰你**，而不是变成噪音源。

---

## 开启配置

```toml
[proactive]
enabled = true
interval_seconds = 900
quiet_hours_start = "23:00"
quiet_hours_end = "08:00"
min_score_to_push = 0.72
max_pushes_per_day = 6
fast_prefilter_enabled = true
fast_prefilter_min_score = 0.4
```

Discord 默认推送频道：

```toml
[discord]
default_push_channel_id = "${DISCORD_DEFAULT_PUSH_CHANNEL_ID}"
```

如果没有配置默认推送频道，主动推送无法稳定发送到目标位置。

---

## 每轮 tick 做什么

```text
ProactiveRuntime.start()
  ↓
每 interval_seconds 秒运行一轮
  ↓
检查 proactive.enabled
  ↓
检查 quiet hours
  ↓
检查今天是否超过 max_pushes_per_day
  ↓
从 ProactiveSourceRegistry 拉候选
  ↓
逐条评估候选
  ↓
推送最高价值或符合阈值的候选
```

一条候选通常包含：

```text
id
title
body / content
source
reason
created_at
metadata
```

`id` 很重要，用于去重。已经推送过的 candidate id 不应重复发送。

---

## 候选来源

候选由 `ProactiveSourceRegistry` 管理。内置来源可以来自：

- 记忆提醒
- 近期上下文
- 手动候选
- Drift 生成的主动跟进候选
- 插件注册的新 source

抽象上，一个 source 的职责很简单：

```text
输入：当前 runtime context
输出：候选列表
```

source 不应该直接决定是否推送。它只负责提供“可能值得推送的东西”。

是否打扰用户，由后面的 prefilter、judge、quota、quiet hours 决定。

---

## Fast prefilter

fast prefilter 是低成本过滤层。

开启配置：

```toml
[proactive]
fast_prefilter_enabled = true
fast_prefilter_min_score = 0.4
```

它的作用是先排掉明显不值得进入完整判断的候选：

```text
候选
  ↓
fast model 或启发式规则
  ↓
score < fast_prefilter_min_score → 丢弃
score >= fast_prefilter_min_score → 进入 judge
```

适合过滤：

- 明显过期的信息
- 与用户长期偏好无关的提醒
- 内容太空泛的候选
- 重复或低价值 follow-up

fast model 不可用时，系统应退回规则判断，而不是中断 proactive runtime。

---

## Judge 打分

judge 是主动推送的核心判断层。

它会结合：

- 候选内容
- `SELF.md`
- `RECENT_CONTEXT.md`
- 当前时间
- 近期上下文
- 候选来源
- 静默和频率约束

输出类似这样的判断：

```text
score: 0.0 - 1.0
should_push: true / false
message: 最终要发给用户的内容
reason: 为什么值得推
```

只有达到配置阈值才推：

```toml
[proactive]
min_score_to_push = 0.72
```

建议默认不要把阈值调得太低。主动推送的错误成本高于被动回复，因为它会主动打断用户。

---

## Quiet hours

```toml
[proactive]
quiet_hours_start = "23:00"
quiet_hours_end = "08:00"
```

quiet hours 用于避免休息时间推送。

判断逻辑：

```text
当前本地时间在 quiet hours 内
  → 本轮不推送

当前本地时间不在 quiet hours 内
  → 继续评估候选
```

如果跨午夜，例如 `23:00` 到 `08:00`，应按夜间区间处理。

---

## Daily cap

```toml
[proactive]
max_pushes_per_day = 6
```

daily cap 控制一天最多主动推送多少条。

```text
今日已推送次数 >= max_pushes_per_day
  → 本轮跳过

今日已推送次数 < max_pushes_per_day
  → 可以继续推送
```

这个限制很重要。即使 judge 认为很多候选都“有价值”，也不应该无限打扰用户。

---

## 去重

主动候选应有稳定 id。

```text
candidate.id
  ↓
检查是否已经推送
  ↓
推过 → 跳过
没推过 → 可继续评估
```

去重的意义：

- 避免同一提醒反复发
- 避免 Drift 生成的同一 follow-up 多次触发
- 避免 source 重启后重复灌入旧候选

---

## 和 Drift 的关系

Drift 不直接等于 proactive。

更准确的关系是：

```text
DriftRuntime
  ↓
空闲时生成、整理或更新某些信息
  ↓
其中一类任务可以产生 proactive candidate
  ↓
ProactiveRuntime 再决定推不推
```

Drift 负责“后台想事情、整理材料、生成候选”。

Proactive 负责“现在要不要打扰用户”。

---

## 插件如何扩展主动推送

插件可以注册新的 proactive source。

典型场景：

- 从 RSS 生成内容候选
- 从日历生成提醒候选
- 从 GitHub issue / PR 生成跟进候选
- 从外部健康数据生成异常提醒
- 从用户自己的任务系统生成 deadline 提醒

source 只返回候选，不直接发消息。

伪代码：

```python
class MyProactiveSource:
    name = "my_source"

    async def collect(self, context):
        return [
            {
                "id": "my-source:task-123",
                "title": "任务即将到期",
                "content": "某任务明天截止",
                "source": "my_source",
                "metadata": {"priority": "high"},
            }
        ]
```

插件注册：

```python
def setup(self, context):
    context.proactive_runtime.sources.register(MyProactiveSource())
```

实际注册接口以当前 `ProactiveSourceRegistry` 代码为准。文档里的伪代码用于说明职责边界。

---

## 调参建议

| 目标 | 调整 |
|------|------|
| 推送太多 | 提高 `min_score_to_push`，降低 `max_pushes_per_day` |
| 推送太少 | 降低 `min_score_to_push`，检查 source 是否有候选 |
| 明显无关内容进入 judge | 提高 `fast_prefilter_min_score` |
| 半夜打扰 | 检查 `quiet_hours_start` / `quiet_hours_end` 和 timezone |
| 重复推送 | 检查 candidate id 是否稳定 |
| 没有任何推送 | 检查 Discord `default_push_channel_id`、proactive.enabled、daily cap、quiet hours |

---

## 当前边界

- 主动推送依赖候选质量。source 给出的内容太差，judge 很难稳定产出好结果。
- fast model 不可用时，判断能力会退回规则或主模型。
- 当前机制是本地单进程状态；多实例部署需要共享去重状态和额度状态。
- 用户反馈闭环还需要继续完善。长期运行时，最好记录“用户是否觉得这条推送有用”，再反向调阈值。
