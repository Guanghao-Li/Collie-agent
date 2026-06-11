# 插件系统

Collie-agent 的插件系统用于在不改 AgentLoop 的情况下扩展能力。

插件可以：

- 注册工具到 `ToolRegistry`
- 监听运行时事件
- 增加 proactive source
- 增加 Drift task
- 访问 memory runtime、message bus、LLM provider 等上下文

---

## 配置

```toml
[plugins]
enabled = true
paths = ["plugins_builtin", "my_plugins"]
strict_plugins = false
```

字段含义：

| 字段 | 说明 |
|------|------|
| `enabled` | 是否启用插件加载 |
| `paths` | 插件搜索目录 |
| `strict_plugins` | 插件加载失败时是否中断启动 |

建议开发阶段保持：

```toml
strict_plugins = false
```

这样单个插件失败不会拖垮主进程。

---

## 插件目录结构

```text
my_plugins/
  my_plugin/
    plugin.py
```

`plugin.py` 需要暴露：

```text
plugin
```

或：

```text
create_plugin()
```

---

## 插件生命周期

典型启动顺序：

```text
main.py
  ↓
build_app_runtime()
  ↓
创建 Settings / MessageBus / EventBus / MemoryRuntime / ToolRegistry
  ↓
PluginManager 加载插件
  ↓
插件拿到 PluginContext
  ↓
插件注册工具、事件监听、proactive source、Drift task
  ↓
发布 StartupEvent
  ↓
启动 Discord / AgentLoop / ProactiveRuntime / DriftRuntime
```

插件应该把注册逻辑放在 `setup()` 或等价入口里。

---

## PluginContext 能访问什么

插件可以通过 context 访问运行时对象：

| 对象 | 用途 |
|------|------|
| `settings` | 读取配置 |
| `workspace` | 访问工作区路径 |
| `event_bus` | 订阅或发布事件 |
| `tool_registry` | 注册工具 |
| `memory_runtime` | 读写或检索记忆 |
| `proactive_runtime` | 注册主动候选来源 |
| `drift_runtime` | 注册空闲任务 |
| `message_bus` | 发送 outbound message |
| `main_llm_provider` | 调主模型 |
| `fast_llm_provider` | 调轻量模型 |

插件不应该直接改内部私有属性。优先使用 context 暴露的 runtime、registry 和 service。

---

## 注册工具

工具适合处理用户明确要求的动作，例如计算、查询、文件操作、记忆管理、外部 API 调用。

工具注册信息通常包括：

- name
- description
- schema
- func

示例：

```python
class CalculatorPlugin:
    name = "calculator_plugin"

    def setup(self, context):
        context.tool_registry.register(
            name="calculator",
            description="计算一个安全的数学表达式",
            schema={
                "type": "object",
                "properties": {
                    "expression": {"type": "string"}
                },
                "required": ["expression"],
            },
            func=self.calculate,
        )

    async def calculate(self, expression: str):
        return {"result": eval(expression)}
```

上面的 `eval` 只是示意，真实工具不能直接执行未校验输入。

工具应该：

- 参数 schema 清晰
- 返回结构化结果
- 对错误做可读包装
- 避免危险副作用
- 对外部 API 设置 timeout
- 写测试覆盖正常路径和错误路径

---

## 监听事件

插件可以监听运行时事件，用于观测、记录、联动。

适合事件监听的场景：

- 启动时初始化资源
- 关闭时释放资源
- 每轮对话后写统计
- 记忆 consolidation 后同步外部系统
- proactive 推送后记录反馈
- Drift task 前后记录运行状态

伪代码：

```python
class MyPlugin:
    def setup(self, context):
        context.event_bus.subscribe("StartupEvent", self.on_startup)
        context.event_bus.subscribe("ShutdownEvent", self.on_shutdown)

    async def on_startup(self, event):
        ...

    async def on_shutdown(self, event):
        ...
```

实际事件名称和订阅接口以当前 `EventBus` 实现为准。

---

## 注册 proactive source

如果插件想让 Agent 主动推送某类信息，不应该直接往 Discord 发消息。

正确做法是注册 proactive source：

```text
外部数据
  ↓
插件 source 转成 candidate
  ↓
ProactiveRuntime prefilter / judge / quota / quiet hours
  ↓
通过后再推送
```

示例场景：

- GitHub PR 需要 review
- 日历事件快开始
- RSS 里出现高相关内容
- 任务系统里有 deadline
- 健康数据出现异常

伪代码：

```python
class GithubReviewSource:
    name = "github_review"

    async def collect(self, context):
        return [
            {
                "id": "github:pr:123",
                "title": "PR 需要 review",
                "content": "repo/name#123 等待你的 review",
                "source": "github_review",
                "metadata": {
                    "repo": "repo/name",
                    "pr": 123,
                },
            }
        ]
```

注册：

```python
def setup(self, context):
    context.proactive_runtime.sources.register(GithubReviewSource())
```

source 只产生候选，不决定是否推送。

---

## 注册 Drift task

如果插件需要低频后台工作，应注册 Drift task。

适合 Drift task：

- 定期整理外部缓存
- 低频同步数据
- 生成主动候选
- 分析最近对话
- 清理过期文件
- 更新索引

伪代码：

```python
class CleanupTask:
    name = "cleanup_task"

    async def should_run(self, context):
        return True

    async def run(self, context):
        # 执行低频后台维护
        return {"cleaned": 3}
```

注册：

```python
def setup(self, context):
    context.drift_runtime.tasks.register(CleanupTask())
```

Drift task 不应该绕过 ProactiveRuntime 直接发主动消息。

---

## 插件错误处理

默认：

```toml
strict_plugins = false
```

插件加载失败时，错误会记录到 `PluginManager.errors`，主程序继续启动。

如果改成：

```toml
strict_plugins = true
```

插件失败会中断启动。

适合 strict 的场景：

- 这个插件是业务核心依赖
- 没有插件时运行会产生错误行为
- 部署环境需要 fail-fast

不适合 strict 的场景：

- 实验性插件
- 第三方数据源不稳定
- 外部 API 偶尔不可用
- 插件只是增强能力

---

## 安全边界

插件拥有很强的运行时能力，因此需要自己控制边界。

建议：

- 不要直接执行用户输入的 shell / Python
- 外部网络请求必须 timeout
- 写文件时限制在 workspace 内
- 对删除、覆盖、发送消息这类动作加确认或阈值
- 不要在插件里硬编码 API key
- 不要把敏感内容写进日志
- 不要绕过 memory / proactive / drift 的既有约束

---

## 测试建议

只改插件系统或工具时，建议至少跑：

```bash
pytest tests/test_plugin_manager.py tests/test_tool_registry.py
```

如果插件注册 proactive source 或 Drift task，还应补对应测试：

```bash
pytest tests/test_proactive_runtime.py tests/test_drift_runtime.py
```

如果插件读写 memory，还应补：

```bash
pytest tests/test_memory_runtime.py tests/test_memory_retriever.py
```

---

## 当前边界

- 插件权限模型还比较粗，插件代码本身拥有较高信任级别。
- 插件隔离不是沙箱级别，不能运行不可信插件。
- 事件、source、task 的具体接口应以当前代码为准。
- 长期可以考虑增加插件权限声明、工具权限、文件访问白名单和网络访问策略。
