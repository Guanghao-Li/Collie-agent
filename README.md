# Collie-agent

Collie-agent 是一个面向个人助理场景的轻量 Agent Runtime。它可以接入 Discord，处理用户消息，维护长期记忆，在合适的时候主动提醒，并在闲时做记忆整理、上下文压缩和反思任务。

这个仓库更像一个可扩展的 Agent 骨架：核心流程保持简单，能力通过配置、工具、插件、记忆和后台任务逐步组合。

## 快速启动

```bash
uv sync --dev
python main.py init
python main.py --config config.toml --workspace ./workspace test-discord
python main.py --config config.toml --workspace ./workspace run
```

如果只想先跑测试：

```bash
pytest
```

## 关键功能

- 被动回复：监听 Discord 消息，组装上下文，调用 LLM，必要时使用工具，然后回复用户。
- 长期记忆：把用户画像、稳定事实、近期上下文和待整理信息分层保存为 Markdown 文件。
- 主动推送：从记忆、近期上下文和候选事件里发现可能值得提醒的内容，并受安静时间、频率和配额约束。
- 闲时任务：在用户不活跃时执行记忆整理、近期上下文压缩、反思和主动提醒候选生成。
- 工具与插件：通过工具注册表和插件系统扩展 Agent 能力。

## 配置入口

主要配置放在 `config.toml`。常用配置包括：

- LLM provider、模型、API key 或本地模型地址。
- Discord token、频道和发送策略。
- Memory workspace、记忆压缩和检索参数。
- Proactive 主动推送开关、安静时间、每日上限。
- Drift 闲时任务开关、空闲阈值、任务频率。
- Plugin 和 tools 的启用方式。

配置字段较多时，不建议只靠 README 理解。请把 README 当入口，细节看文档。

## 文档

- [说明文档](docs/说明文档.md)：从功能抽象角度说明 Collie-agent 能做什么、各模块如何协作，适合先读。
- [教学文档](docs/教学文档.md)：从工程实现角度拆解 runtime、memory、pipeline、idle tasks 和扩展点，适合开发和改造时阅读。

## 常用命令

```bash
python main.py init
python main.py run
python main.py memory
python main.py test-discord
pytest
```

## 项目结构

```text
agent/        # 被动回复主循环
bootstrap/    # 应用装配和启动流程
memory/       # 记忆运行时、Markdown store、检索和整理
proactive/    # 主动推送候选、判断和发送
drift/        # 闲时后台任务
plugins/      # 插件加载和插件上下文
tools/        # 工具注册与调用
docs/         # 说明文档与教学文档
```

## 当前设计原则

Collie-agent 优先保证默认行为稳定：没有真实 embedding 服务时，向量记忆默认关闭；Markdown 记忆文件保持可读；旧格式文件继续兼容；新增能力通过协议和扩展点接入，而不是强行替换已有调用方。
