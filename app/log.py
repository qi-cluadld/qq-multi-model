"""日志：控制台单行彩色事件 + 滚动文件（logs/bot.log，UTF-8 全量）。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import ui

#: 把模块名映射成给人看的短标签
LABELS = {
    "bot.chat": "对话",
    "bot.onebot": "QQ",
    "bot.qqofficial": "QQ",
    "bot.llm": "模型",
    "bot.napcat": "安装",
    "bot.net": "网络",
    "bot.deepseek": "模型",
    "bot": "系统",
}


class _SafeStreamHandler(logging.StreamHandler):
    """Windows 控制台可能是 GBK，写不进去就降级，不能让日志把程序搞崩。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except UnicodeEncodeError:
            try:
                msg = self.format(record)
                encoding = getattr(self.stream, "encoding", None) or "utf-8"
                self.stream.write(msg.encode(encoding, "replace").decode(encoding))
                self.stream.write(self.terminator)
                self.stream.flush()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            self.handleError(record)


class ConsoleFormatter(logging.Formatter):
    """`14:03:22  ◆ 消息  张三：你好` —— 短、对齐、按级别着色。"""

    def format(self, record: logging.LogRecord) -> str:
        label = LABELS.get(record.name) or record.name.rsplit(".", 1)[-1]
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)

        if record.levelno >= logging.ERROR:
            icon, paint = "✕", ui.err
        elif record.levelno >= logging.WARNING:
            icon, paint = "▲", ui.warn
        elif record.levelno <= logging.DEBUG:
            icon, paint = "·", ui.dim
        else:
            icon, paint = "·", ui.dim

        stamp = self.formatTime(record, "%H:%M:%S")
        head = f"{ui.dim(stamp)}  {paint(icon)} {ui.dim(ui.pad(label, 4))}"
        text = message if record.levelno < logging.WARNING else paint(message)
        return f"{head} {text}"


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = _SafeStreamHandler(stream=sys.stdout)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "bot.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(file_handler)
    except OSError as exc:  # 只读目录之类，不致命
        print(ui.warn(f"无法写入日志文件（{exc}），只输出到控制台。"))

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return logging.getLogger("bot")
