"""多模型 QQ 机器人 —— 程序入口。

用法：
    qq-multi-model.exe                 # 用 exe 同目录的 config.yaml 启动
    qq-multi-model.exe --config x.yaml
    qq-multi-model.exe --check         # 只检查配置，不启动
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import sys
import time
from pathlib import Path

import yaml

from app import __version__, ui
from app.chat import ChatService
from app.config import app_dir, load_config
from app.llm import LLMClient
from app.log import setup_logging

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qq-multi-model",
        description="把 DeepSeek 接到 QQ 上（支持 NapCat/OneBot 和 QQ 官方机器人）",
    )
    parser.add_argument("-c", "--config", help="配置文件路径，默认用 exe 同目录的 config.yaml")
    parser.add_argument("--setup", action="store_true", help="重新跑一遍配置向导")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--check", action="store_true", help="只校验配置并打印，不启动")
    parser.add_argument("-v", "--version", action="version", version=f"qq-multi-model {__version__}")
    return parser


async def _run(cfg, logger) -> None:
    client = LLMClient(cfg)
    chat = ChatService(cfg, client)
    backend = cfg.get("backend", "onebot")
    model = client.default_model()

    try:
        if backend == "onebot":
            from app.onebot import OneBotServer

            logger.info("后端：OneBot v11（NapCat / Lagrange），默认模型：%s", model)
            await OneBotServer(cfg, chat).run()
        elif backend == "qqofficial":
            from app.qqofficial import QQOfficialBot

            logger.info("后端：QQ 官方机器人，默认模型：%s", model)
            await QQOfficialBot(cfg, chat).run()
        else:
            raise SystemExit(f"backend={backend!r} 不支持，只能是 onebot 或 qqofficial")
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config).expanduser().resolve() if args.config else app_dir() / "config.yaml"
    ui.set_title("qq-multi-model · 多模型 QQ 机器人")

    if args.check:
        if not cfg_path.exists():
            print(ui.warn(f"还没有配置文件：{cfg_path}"))
            print(ui.dim("       先不带 --check 运行一次，会进入配置向导。"))
            return 1
        cfg = load_config(args.config)
        print(f"配置文件：{cfg.path}")
        print(yaml.safe_dump(cfg.masked(), allow_unicode=True, sort_keys=False))
        print(ui.ok("配置加载完成。"))
        return 0

    print()
    print(f"  {ui.bold(ui.accent('qq-multi-model'))}  {ui.dim('v' + __version__)}")

    # 没有配置文件（或显式 --setup）就进向导，而不是让用户自己去改 YAML
    if args.setup or not cfg_path.exists():
        from app import wizard

        if not wizard.is_interactive():
            print(ui.warn("找不到 config.yaml，而且当前不是交互式终端，没法进入配置向导。"))
            print(ui.dim("       请直接双击程序（会弹出窗口），或在终端里运行；"))
            print(ui.dim("       也可以把 config.example.yaml 复制成 config.yaml 手动填写。"))
            return 1

        if not wizard.run_setup(cfg_path):
            return 0
        args.config = str(cfg_path)
        print()

    cfg = load_config(args.config)
    logger = setup_logging(app_dir() / "logs", args.log_level)
    ui.STATS.reset()

    print()
    print(ui.banner(
        __version__,
        str(cfg.get("defaults.model", "")),
        str(cfg.get("backend", "onebot")),
        str(cfg.path),
        str(app_dir() / "logs" / "bot.log"),
    ))
    print()

    exit_code = 0
    try:
        asyncio.run(_run(cfg, logger))
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        if exc.code:
            print()
            print(ui.err(f"启动失败：{exc}"))
            exit_code = 1
    finally:
        print()
        print(ui.summary(time.time() - ui.STATS.started))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
