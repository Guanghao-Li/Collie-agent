# Drift 原理分析

## 1. Drift 是什么

Drift 在 Collie-agent 里表示“空闲时的后台思考和整理任务”。

普通聊天机器人只在用户发消息时工作。用户不说话，它就什么都不做。

Collie-agent 希望更像一个长期运行的个人助理：即使用户暂时不互动，它也可以在后台做一些低优先级工作。

例如：

- 整理待处理记忆。
- 总结近期对话。
- 写一条阶段性反思。
- 生成主动推送候选。
- 衰减长期没用的低价值记忆。

这些事情就由 `DriftRuntime` 负责。

## 2. 为什么需要 Drift

有些工作不适合在用户等待回复时做。

比如整理大量记忆，如果放在用户提问时执行，回复会变慢。

更好的方式是：

- 用户说话时，优先快速回应。
- 用户空闲时，再做整理、总结、反思等后台工作。

这就是 Drift 的意义。

## 3. DriftRuntime 负责什么

`DriftRuntime` 是空闲后台任务的调度器。

它负责：

- 判断用户是否空闲。
- 定期运行 drift cycle。
- 从 `DriftTaskRegistry` 中读取任务。
- 判断每个任务是否应该运行。
- 按配置限制每轮最多运行几个任务。
- 发布 Drift 相关事件。
- 记录上次运行时间。

专业名词解释：

- “调度器”：决定什么时候运行哪些任务的模块。
- “cycle”：一次完整检查和执行过程。

## 4. 什么叫“用户空闲”

配置里有：

```toml
[drift]
run_only_when_idle = true
idle_after_seconds = 600
```

意思是：

如果用户 600 秒内没有发消息，就认为用户空闲。

如果 `run_only_when_idle = true`，那么用户刚刚互动过时，Drift 不会运行。

## 5. 为什么要等用户空闲

因为 Drift 是低优先级任务。

用户正在聊天时，系统应该优先处理用户消息，而不是抢资源做后台整理。

技术小白可以这样理解：

- 用户正在说话：助理先认真听和回答。
- 用户不说话了：助理再整理笔记、复盘、准备提醒。

## 6. DriftTask 是什么

`DriftTask` 是一个后台任务。

每个 task 需要提供：

- `name`：任务名称。
- `interval_seconds`：任务建议间隔。
- `should_run(ctx)`：判断是否应该运行。
- `run(ctx)`：真正执行任务。

专业名词解释：

- “task”：任务。
- “context”：上下文对象，里面放着任务需要用到的系统能力。

## 7. DriftContext 是什么

`DriftContext` 是 Drift task 执行时拿到的工具包。

它包含：

- `memory_runtime`
- `session_manager`
- `proactive_runtime`
- `llm_provider`
- `current_time`
- `last_user_activity_at`
- `metadata`

也就是说，一个 Drift task 可以读记忆、读会话、生成主动推送候选、调用 LLM。

## 8. DriftResult 是什么

每个 Drift task 执行后返回 `DriftResult`。

它包含：

- `task_name`：任务名称。
- `success`：是否成功。
- `summary`：执行摘要。
- `created_candidates`：创建了多少主动推送候选。
- `updated_memories`：更新了多少记忆。
- `metadata`：额外信息。

这个结果可以用于日志、命令输出或未来的监控面板。

## 9. DriftTaskRegistry：任务注册表

`DriftTaskRegistry` 保存所有可运行的 Drift task。

插件可以通过它注册新任务。

例如内置 `drift_plugin` 会注册：

- `MemoryConsolidationTask`
- `RecentContextSummaryTask`
- `ReflectionTask`
- `ProactiveIdeaTask`
- `MemoryDecayTask`

## 10. MemoryConsolidationTask

这个任务负责整理待处理记忆。

它会检查：

```text
pending memory 数量是否大于 0
```

如果有待整理记忆，就调用：

```python
MemoryRuntime.consolidate()
```

它的作用是把临时候选记忆变成稳定长期记忆。

## 11. RecentContextSummaryTask

这个任务负责总结近期会话。

它会读取最近几个 session，把最近消息拼成摘要，并写入：

```text
RECENT_CONTEXT.md
```

这个文件会在未来对话和主动推送判断中被使用。

## 12. ReflectionTask

这个任务负责写轻量反思。

当前版本的反思比较简单，会记录当前有多少活跃记忆、多少待整理记忆。

未来可以扩展成更智能的反思，例如：

- 最近用户关注什么。
- 当前项目有什么风险。
- 哪些目标长时间没有推进。
- 哪些记忆可能过期。

## 13. ProactiveIdeaTask

这个任务会生成主动推送候选。

注意，它不会直接发消息给用户。

它只把候选内容放到 `ManualCandidateSource` 里，之后仍然要交给 `ProactiveRuntime` 判断。

这样设计可以避免 Drift 绕过主动推送规则。

## 14. MemoryDecayTask

这个任务会降低长期未使用、低重要性、低置信度记忆的置信度。

专业名词解释：

- “decay”：衰减。
- “置信度衰减”：系统逐渐降低对旧信息可靠性的信任。

为什么需要衰减？

因为用户的信息会变化。很久没被用过、又不太重要的记忆，不应该永远保持高权重。

## 15. 一次 Drift cycle 的流程

```text
DriftRuntime 定时醒来
  -> 判断是否启用
  -> 判断用户是否空闲
  -> 创建 DriftContext
  -> 遍历 DriftTaskRegistry
  -> 调用 task.should_run(ctx)
  -> 如果应该运行，调用 task.run(ctx)
  -> 收集 DriftResult
  -> 达到 max_tasks_per_cycle 后停止
  -> 更新 last_run_at
```

## 16. max_tasks_per_cycle 是什么

配置：

```toml
max_tasks_per_cycle = 2
```

意思是每次 Drift cycle 最多运行 2 个任务。

这样可以避免一次空闲检查做太多工作。

## 17. Drift 和 AgentLoop 的关系

`AgentLoop` 负责用户消息。

`DriftRuntime` 负责后台任务。

它们是分开的。

这很重要，因为如果把所有后台逻辑都塞进 AgentLoop，AgentLoop 会变得非常复杂，用户消息处理也会变慢。

当前结构是：

```text
用户消息 -> AgentLoop
空闲整理 -> DriftRuntime
```

## 18. Drift 和记忆系统的关系

Drift 经常会使用记忆系统。

例如：

- 整理 pending memories。
- 更新 recent context。
- 写 reflections。
- 衰减旧记忆。

可以说 Drift 是记忆系统的“后台维护人员”。

## 19. Drift 和 Proactive 的关系

Drift 可以帮助 Proactive 产生候选内容。

例如：

```text
Drift 发现某个目标很久没推进
  -> 生成一个候选提醒
  -> 交给 Proactive 判断
  -> 如果合适，再推送给用户
```

Drift 不直接打扰用户，Proactive 才负责最后的“要不要发出去”。

## 20. 技术小白类比

你可以把 Drift 想象成一个办公室助理下班前做的整理工作：

- 把桌上的便签分类。
- 把会议记录整理成摘要。
- 写一条今天的工作复盘。
- 找出明天可能要提醒老板的事项。
- 把过期资料放到低优先级区。

这些事不是当场回答问题，但会让第二天工作更顺。

## 21. 当前版本的局限

当前 Drift 是原型级实现：

- task 的 interval_seconds 还没有做复杂调度。
- ReflectionTask 比较简单。
- RecentContextSummaryTask 只是拼接近期消息，不是高级摘要。
- ProactiveIdeaTask 只是生成简单候选。
- MemoryDecayTask 规则比较保守。

但这些限制也是扩展点。

## 22. 怎么新增 Drift task

新增一个 Drift task 需要实现四个部分：

```python
class MyTask:
    name = "my_task"
    interval_seconds = 3600

    async def should_run(self, ctx):
        return True

    async def run(self, ctx):
        return DriftResult(
            task_name=self.name,
            success=True,
            summary="已完成。",
        )
```

然后注册：

```python
await context.drift_runtime.add_task(MyTask())
```

## 23. Drift 的核心价值

Drift 让 Agent 有了“后台生命周期”。

它让 Agent 不只是响应消息，而是在用户不互动时继续维护自己的状态。

对个人 Agent 来说，这很关键：

- 记忆需要整理。
- 上下文需要压缩。
- 旧信息需要降权。
- 主动提醒需要准备候选。
- 系统需要在不打扰用户的情况下变得更有条理。

这就是 Drift 在 Collie-agent 中的意义。

