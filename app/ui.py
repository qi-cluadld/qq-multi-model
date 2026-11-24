"""控制台外观：Windows 开 ANSI、统一配色、画面板和状态行。

目标不是花哨，而是「一眼能看懂」：
- 启动时一块信息面板（版本 / 模型 / 接入方式 / 配置 / 日志路径）
- 运行中一行一条事件，时间 + 图标 + 类别对齐，按级别着色
- 退出时一块小结（运行时长 / 收消息 / 回复 / 出错次数）
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------- 控制台准备

def prepare_console() -> None:
    """把控制台切到 UTF-8；切不动就靠后面的 ASCII 降级兜底。

    Windows 默认代码页是 936（GBK），而圆角边框 ╭╮╰╯ 不在 GBK 里，
    不处理的话会打出乱码，极端情况下 print 直接抛 UnicodeEncodeError。
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _can_encode(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return True
    except Exception:  # noqa: BLE001
        return False


prepare_console()

#: 终端编码放得下圆角框就用圆角，放不下就用 ASCII —— 宁丑不崩
_BOX = (
    {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│", "ml": "├", "mr": "┤"}
    if _can_encode("╭─╮│├┤╰╯")
    else {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|", "ml": "+", "mr": "+"}
)


# ---------------------------------------------------------------- 颜色

RESET = "\033[0m"
_ACCENT = "\033[38;2;77;107;254m"    # 蓝
_OK = "\033[38;2;52;199;123m"        # 绿
_WARN = "\033[38;2;255;159;10m"      # 橙
_ERR = "\033[38;2;255;69;58m"        # 红
_DIM = "\033[38;2;139;148;178m"      # 灰
_TEXT = "\033[38;2;230;234;245m"     # 近白
_BOLD = "\033[1m"


def _enable_vt() -> bool:
    """Windows 10+ 打开 ANSI 转义；其它平台默认支持。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


_VT = _enable_vt()


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return _VT and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


_COLOR = color_enabled()


def c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _COLOR else text


def accent(t: str) -> str:
    return c(t, _ACCENT)


def ok(t: str) -> str:
    return c(t, _OK)


def warn(t: str) -> str:
    return c(t, _WARN)


def err(t: str) -> str:
    return c(t, _ERR)


def dim(t: str) -> str:
    return c(t, _DIM)


def bold(t: str) -> str:
    return c(t, _BOLD)


def set_title(text: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 计数

@dataclass
class Stats:
    started: float = 0.0
    messages: int = 0
    replies: int = 0
    errors: int = 0

    def reset(self) -> None:
        self.started = time.time()
        self.messages = 0
        self.replies = 0
        self.errors = 0


STATS = Stats()


# ---------------------------------------------------------------- 宽度

def dwidth(text: str) -> int:
    """按终端列宽算长度：中文/全角算 2 列。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def term_width(default: int = 80) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:  # noqa: BLE001
        return default


def _pad(text: str, width: int) -> str:
    diff = width - dwidth(text)
    return text + " " * max(0, diff)


def pad(text: str, width: int) -> str:
    """公开版本，供日志格式化对齐标签用。"""
    return _pad(text, width)


# ---------------------------------------------------------------- 面板

def _vlen(text: str) -> int:
    """可见宽度：先去掉 ANSI 转义，再按东亚宽度算。"""
    import re

    return dwidth(re.sub(r"\x1b\[[0-9;]*m", "", text))


def _fill(text: str, width: int) -> str:
    return " " * max(0, width - _vlen(text))


# ---------------------------------------------------------------- 面板

def panel(title: str, rows: list[tuple[str, str]], footer: str = "",
          width: int | None = None) -> str:
    """画一个圆角面板；rows 是 (左列, 右列)。返回多行字符串。"""
    b = _BOX
    total = width or min(max(term_width() - 2, 56), 92)
    inner = total - 4  # 两侧 │ 和空格占 4
    rule = b["h"] * (total - 2)

    lines = [f"{b['tl']}{rule}{b['tr']}"]
    if title:
        painted = bold(accent(title))
        lines.append(f"{b['v']} {painted}{_fill(title, inner)} {b['v']}")
        if rows or footer:
            lines.append(f"{b['ml']}{rule}{b['mr']}")
    if rows:
        label_w = max(dwidth(k) for k, _ in rows) + 2
        for key, value in rows:
            painted = f"{dim(_pad(key, label_w))}{value}"
            lines.append(f"{b['v']} {painted}{_fill(_pad(key, label_w) + value, inner)} {b['v']}")
        if footer:
            lines.append(f"{b['ml']}{rule}{b['mr']}")
    if footer:
        lines.append(f"{b['v']} {dim(footer)}{_fill(footer, inner)} {b['v']}")
    lines.append(f"{b['bl']}{rule}{b['br']}")
    return "\n".join(lines)


def event(kind: str, text: str, level: str = "info") -> str:
    """一行事件：时间 + 图标 + 类别 + 内容。"""
    stamp = time.strftime("%H:%M:%S")
    icons = {"info": ("·", _DIM), "msg": ("◆", _ACCENT), "ok": ("●", _OK),
             "warn": ("▲", _WARN), "err": ("✕", _ERR)}
    icon, code = icons.get(kind, icons["info"])
    label = {"msg": "消息", "ok": "连接", "warn": "注意", "err": "错误"}.get(kind, "系统")
    return f"{dim(stamp)}  {c(icon, code)} {dim(_pad(label, 4))} {text}"


def summary(duration: float) -> str:
    mins = int(duration // 60)
    hours = mins // 60
    span = f"{hours} 小时 {mins % 60} 分" if hours else f"{mins} 分 {int(duration % 60)} 秒"
    rows = [
        ("运行时长", span),
        ("收到消息", f"{STATS.messages} 条"),
        ("回复消息", f"{STATS.replies} 条"),
        ("出错", f"{STATS.errors} 次"),
    ]
    return panel("已停止", rows, footer=dim("按 Ctrl+C 随时可以再次退出"))


def banner(version: str, model: str, backend: str, config_path: str, log_path: str) -> str:
    backend_label = {
        "onebot": "NapCat / OneBot v11",
        "qqofficial": "QQ 官方机器人",
    }.get(backend, backend)
    rows = [
        ("模型", accent(model)),
        ("接入", backend_label),
        ("配置", dim(config_path)),
        ("日志", dim(log_path)),
    ]
    return panel(f"qq-multi-model v{version}", rows,
                 footer=dim("按 Ctrl+C 退出  ·  改了配置要重启才生效"))
