# Collie-agent 配置说明

默认配置已经集中在根目录的 `config.example.toml`。

新手只需要运行：

```bash
python main.py init
```

然后编辑生成出来的 `config.toml`。常用项、Discord、LLM、记忆、主动推送、Drift 和插件配置都已经在这一份文件里。

## 推荐流程

1. 先保持 `llm.provider = "echo"`。
2. 设置 Discord 环境变量。
3. 运行 `python main.py --config config.toml test-discord`。
4. 确认 Discord 配置没问题后，再运行 `python main.py --config config.toml run`。
5. 需要真实大模型时，把 `llm.provider` 改成 `"openai-compatible"`，并设置 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`。

## 常用环境变量

```powershell
$env:DISCORD_BOT_TOKEN="你的 Discord Bot Token"
$env:DISCORD_GUILD_ID="你的 Discord 服务器 ID"
$env:DISCORD_DEFAULT_PUSH_CHANNEL_ID="默认推送频道 ID"
$env:LLM_MODEL="厂商提供的模型名"
$env:LLM_BASE_URL="厂商提供的 OpenAI 兼容 API 地址"
$env:LLM_API_KEY="你的 LLM API Key"
$env:FAST_LLM_API_KEY="你的 fast model API Key"
```

环境变量只在当前 PowerShell 窗口里临时生效。关掉窗口后，下次运行前需要重新设置。

## 关键字段

```toml
[discord]
enabled = true
bot_token = "${DISCORD_BOT_TOKEN}"
guild_id = "${DISCORD_GUILD_ID}"
allowed_channel_ids = []
allowed_user_ids = []
default_push_channel_id = "${DISCORD_DEFAULT_PUSH_CHANNEL_ID}"
```

- `allowed_channel_ids` 留空表示不限制频道。
- `allowed_user_ids` 留空表示不限制用户，长期使用时建议填上你自己的用户 ID。
- `default_push_channel_id` 是主动推送默认发送的频道。

```toml
[llm]
provider = "echo"
```

- `"echo"`：本地测试，不联网，不需要 API key。
- `"openai-compatible"`：使用 OpenAI 兼容接口，会读取 `[llm.compatible]`。

```toml
[llm.compatible]
model = "${LLM_MODEL}"
api_key = "${LLM_API_KEY}"
base_url = "${LLM_BASE_URL}"
timeout_seconds = 30
temperature = 0.7
```

如果使用第三方 OpenAI 兼容服务，通常只需要改 `model` 和 `base_url`。

## 可选：拆分配置

`configs/` 目录里的这些文件只作为进阶示例和兼容保留：

- `base.toml`
- `discord.example.toml`
- `llm.echo.toml`
- `llm.compatible.example.toml`
- `llm.openai.example.toml`（旧名称兼容入口）
- `local.example.toml`
- `production.example.toml`

配置加载器仍然支持 `extends`。如果你想把配置拆成多个文件，可以这样写：

```toml
[config]
extends = [
  "configs/base.toml",
  "configs/discord.example.toml",
  "configs/llm.echo.toml",
]
```

后面的文件会覆盖前面的同名字段。多数情况下，直接使用根目录的 `config.toml` 更简单。
