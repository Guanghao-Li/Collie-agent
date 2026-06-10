# Proactive 原理分析

## 1. Proactive 是什么

Proactive 的意思是“主动的”。

在普通聊天机器人里，用户问一句，机器人答一句。机器人不会自己找你。

在 Collie-agent 里，ProactiveRuntime 让 Agent 拥有一点主动性：它可以定期检查是否有值得提醒用户的事情，然后主动发到 Discord。

简单说：

```text
普通机器人：你问，我答。
Proactive Agent：你不问，我也可能在合适的时候提醒你。
```

## 2. 为什么个人 Agent 需要主动性

个人助理不应该只会被动回答。

例如：

- 你有一个长期目标。
- 你最近提到一个项目。
- 你之前说过需要跟进某件事。
- 最近上下文里有未完成事项。

一个更像助理的 Agent 应该能在合适的时候提醒你，而不是等你自己想起来。

## 3. ProactiveRuntime 负责什么

`ProactiveRuntime` 是主动推送系统的核心。

它负责：

- 定期运行检查。
- 从多个 source 获取候选内容。
- 调用 judge 判断是否值得推送。
- 检查 quiet hours。
- 检查每日推送上限。
- 避免重复推送。
- 把通过判断的内容发到 MessageBus。

## 4. Source 是什么

Source 是“候选内容来源”。

专业名词解释：

- “候选内容”：可能值得推送，但还没确定要推送的信息。
- “Source”：产生候选内容的模块。

例如：

- 从长期记忆里找目标和项目。
- 从近期上下文里找待跟进事项。
- 测试时手动放入一个候选内容。

当前内置 source：

- `MemoryReminderSource`
- `RecentContextSource`
- `ManualCandidateSource`

## 5. ProactiveCandidate 是什么

每个 source 返回的是 `ProactiveCandidate`。

它包含：

- `id`：候选内容唯一编号。
- `source`：来源。
- `title`：标题。
- `content`：正文。
- `url`：可选链接。
- `created_at`：创建时间。
- `metadata`：额外信息。

你可以把 candidate 理解成“待审核提醒”。

## 6. MemoryReminderSource

`MemoryReminderSource` 会搜索记忆中的目标、项目和事件。

例如记忆里有：

```text
用户正在构建 Collie-agent 项目
```

它可能生成一个候选内容：

```text
标题：跟进 project
内容：用户正在构建 Collie-agent 项目
```

这个候选内容不会立刻发送，还要经过 judge 判断。

## 7. RecentContextSource

`RecentContextSource` 会读取 `RECENT_CONTEXT.md`。

如果近期上下文足够长，它会把近期上下文作为候选提醒来源。

例如：

```text
近期对话多次提到要完善文档和测试。
```

它可能生成：

```text
标题：近期上下文跟进
内容：近期对话多次提到要完善文档和测试。
```

## 8. ManualCandidateSource

`ManualCandidateSource` 主要用于测试或手动注入候选内容。

例如测试里可以写：

```python
source.add_candidate("项目提醒", "跟进 Agent 项目。")
```

这让测试不需要真实外部信息源，也能验证主动推送流程。

## 9. Judge 是什么

Judge 是“判断器”。

它负责判断一个 candidate 是否值得推送。

专业名词解释：

- “judge”：判断模块。
- “score”：分数。分数越高，越可能值得推送。
- “threshold”：阈值。只有分数超过阈值，才通过。

在当前版本中，`ProactiveJudge` 使用简化启发式规则。

## 10. 启发式规则是什么

“启发式规则”就是经验规则，不是完美智能判断。

例如：

- 候选内容包含“目标”“项目”“跟进”“提醒”等词，加分。
- 候选内容和用户画像或近期上下文有重合词，加分。
- 来源是 manual 或 memory_reminder，加分。

最后得到一个 0 到 1 之间的分数。

如果分数大于 `min_score_to_push`，就可能推送。

## 11. 为什么不直接所有内容都推送

主动推送如果太频繁，会变成打扰。

所以系统必须克制。

Collie-agent 有几道门：

1. 是否在 quiet hours。
2. 今日推送次数是否达到上限。
3. 这个 candidate 是否已经推送过。
4. judge 分数是否达到阈值。

这些检查能避免 Agent 过度主动。

## 12. Quiet hours 是什么

Quiet hours 是“安静时间”。

配置示例：

```toml
[proactive]
quiet_hours_start = "23:00"
quiet_hours_end = "08:00"
```

意思是晚上 23:00 到早上 08:00 不主动推送。

专业名词解释：

- “quiet hours”：不希望被打扰的时间段。

## 13. 每日推送上限

配置：

```toml
max_pushes_per_day = 6
```

意思是一天最多主动推送 6 次。

这是为了防止 Agent 变成消息轰炸器。

## 14. 推送阈值

配置：

```toml
min_score_to_push = 0.72
```

如果 judge 给出的分数低于 0.72，就不推送。

阈值越高，Agent 越谨慎。阈值越低，Agent 越主动。

## 15. 推送如何进入 Discord

当一个 decision 通过判断后：

1. `ProactiveRuntime.push()` 创建 `OutboundMessage`。
2. 消息进入 `MessageBus.outbound`。
3. `DiscordChannel` 从 outbound 队列读取消息。
4. `DiscordChannel` 发送到 `default_push_channel_id`。

ProactiveRuntime 不直接调用 Discord API。

这样设计是为了保持解耦。

## 16. ProactiveDecision 是什么

Judge 输出的是 `ProactiveDecision`。

它包含：

- `candidate`：原始候选内容。
- `should_push`：是否应该推送。
- `score`：评分。
- `reason`：原因。
- `message`：最终推送文本。

这个对象让系统不只是知道“推不推”，还知道“为什么”。

## 17. 主动推送流程图

```text
ProactiveRuntime 定时醒来
  -> 遍历所有 Source
  -> 获取 Candidate
  -> ProactiveJudge 打分
  -> 检查 quiet hours
  -> 检查每日上限
  -> 检查是否重复
  -> 通过则发布 OutboundMessage
  -> DiscordChannel 发送到 Discord
```

## 18. 和记忆系统的关系

Proactive 依赖记忆系统。

它会读取：

- 用户画像。
- 近期上下文。
- 与候选内容相关的长期记忆。

没有记忆，主动推送就很难个性化。

例如：

- 用户正在做项目 A，提醒项目 A。
- 用户有长期目标 B，提醒目标 B。
- 用户喜欢少打扰，就降低推送频率。

## 19. 和 Drift 的关系

Drift 可以生成 proactive candidate。

例如 `ProactiveIdeaTask` 会在空闲时根据记忆和近期上下文生成一个候选提醒，但它不会直接发给用户，而是交给 ProactiveRuntime 再判断。

这样可以避免后台任务绕过主动推送的克制机制。

## 20. 技术小白类比

你可以把 Proactive 系统想象成一个助理每天定时检查便签：

1. 助理先翻便签和用户档案。
2. 找出可能要提醒的事情。
3. 判断现在适不适合提醒。
4. 判断今天提醒次数会不会太多。
5. 如果确实重要，就发消息。

这个助理不是想到什么就说什么，而是先过几道筛子。

## 21. 当前版本的局限

当前 Proactive 还是简化版：

- 没有接入日历。
- 没有接入邮件。
- 没有接入 RSS 或新闻。
- 没有复杂 LLM judge。
- 没有用户反馈学习机制。

但架构已经预留了扩展点。以后可以新增 source，例如：

- CalendarSource
- EmailSource
- RSSSource
- GitHubIssueSource
- ReminderSource

## 22. 怎么扩展一个 Source

一个 source 只需要有 `name` 和 `fetch()`。

示例：

```python
class MySource:
    name = "my_source"

    async def fetch(self):
        return [
            ProactiveCandidate(
                source=self.name,
                title="提醒",
                content="这是一条候选提醒。",
            )
        ]
```

然后注册：

```python
await context.proactive_runtime.add_source(MySource())
```

## 23. Proactive 的核心价值

Proactive 让 Agent 从“被动工具”变成“主动助理”。

但主动性必须有边界。

Collie-agent 用 source、judge、quiet hours、每日上限和去重机制，让主动推送尽量做到：

- 有上下文。
- 有克制。
- 可解释。
- 可扩展。

