from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import shutil

from bootstrap.app import build_app_runtime
from bootstrap.config import load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collie-agent")
    parser.add_argument("--config", default="config.toml", help="config.toml 配置文件路径")
    parser.add_argument("--workspace", default="./workspace", help="工作区目录")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="创建 config.toml 和工作区目录")
    subcommands.add_parser("run", help="运行 Agent")
    subcommands.add_parser("memory", help="打印记忆文件内容")
    subcommands.add_parser("test-discord", help="检查 Discord 配置")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    workspace = Path(args.workspace)

    if args.command == "init":
        if not config_path.exists():
            shutil.copyfile(Path(__file__).with_name("config.example.toml"), config_path)
            print(f"已创建配置文件：{config_path}")
        workspace.mkdir(parents=True, exist_ok=True)
        print(f"工作区已就绪：{workspace}")
        return

    config = load_config(config_path)
    runtime = build_app_runtime(config, workspace)

    if args.command == "run":
        await runtime.run()
        return

    if args.command == "memory":
        await runtime.memory_runtime.initialize()
        print(await runtime.memory_runtime.read_profile())
        print(await runtime.memory_runtime.read_core_memory())
        print(await runtime.memory_runtime.read_recent_context())
        await runtime.llm_provider.close()
        return

    if args.command == "test-discord":
        if not config.discord.enabled:
            print("Discord 已禁用。")
        elif not config.discord.bot_token:
            print("Discord 已启用，但 bot_token 为空。")
        else:
            print("Discord 配置中已有 bot token。只有执行 run 时才会尝试连接网络。")
        await runtime.llm_provider.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("正在关闭。")


if __name__ == "__main__":
    main()
