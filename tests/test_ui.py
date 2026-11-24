"""控制台外观测试：对齐、配色开关、日志格式。

    python tests/test_ui.py
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import ui  # noqa: E402
from app.log import ConsoleFormatter  # noqa: E402

PASSED = 0
FAILED: list[str] = []
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def plain(text: str) -> str:
    return ANSI.sub("", text)


def with_color(enabled: bool):
    old = ui._COLOR
    ui._COLOR = enabled
    return old


def main() -> int:
    print("[1] 宽度计算")
    check("纯 ASCII", ui.dwidth("ABC") == 3)
    check("中文算 2 列", ui.dwidth("中文") == 4)
    check("中英混排", ui.dwidth("中文ABC") == 7)
    check("全角标点算 2 列", ui.dwidth("：") == 2)

    print("\n[2] 面板对齐")
    old = with_color(False)
    try:
        block = ui.panel("标题中英混排", [
            ("模型", "deepseek/deepseek-flash"),
            ("配置", r"D:\a\b\c.yaml"),
        ], footer="按 Ctrl+C 退出")
        widths = {ui._vlen(line) for line in block.splitlines()}
        check("面板每行等宽", len(widths) == 1, str(sorted(widths)))
        check("面板有边框", block.startswith(ui._BOX["tl"]) and block.endswith(ui._BOX["br"]))
        for w in (56, 78, 92):
            block_w = ui.panel("t", [("键", "值")], width=w)
            widths_w = {ui._vlen(line) for line in block_w.splitlines()}
            check(f"指定宽度 {w} 时每行等宽", widths_w == {w}, str(sorted(widths_w)))
    finally:
        with_color(old)

    print("\n[2b] 编码兜底（强制 ASCII 边框）")
    old = with_color(False)
    saved = ui._BOX
    try:
        ui._BOX = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|", "ml": "+", "mr": "+"}
        block = ui.panel("t", [("模型", "deepseek/deepseek-flash")], footer="f", width=60)
        widths = {ui._vlen(line) for line in block.splitlines()}
        check("ASCII 边框仍然等宽", widths == {60}, str(sorted(widths)))
        check("ASCII 边框不含全角框线", not any(ch in "╭╮╰╯│─├┤" for ch in block))
    finally:
        ui._BOX = saved
        with_color(old)

    print("\n[3] 启动面板内容")
    old = with_color(False)
    try:
        banner = ui.banner("2.2.0", "deepseek/deepseek-flash", "onebot", "cfg.yaml", "bot.log")
        check("包含版本号", "v2.2.0" in banner)
        check("包含默认模型", "deepseek/deepseek-flash" in banner)
        check("后端名可读（不是 onebot 原文）", "NapCat / OneBot v11" in banner, banner[:80])
        check("包含配置与日志路径", "cfg.yaml" in banner and "bot.log" in banner)
    finally:
        with_color(old)

    print("\n[4] 退出小结")
    old = with_color(False)
    try:
        ui.STATS.messages, ui.STATS.replies, ui.STATS.errors = 42, 40, 1
        text = ui.summary(4980)
        check("秒级时长", "1 分" in ui.summary(75), ui.summary(75))
        check("小时级时长", "1 小时 23 分" in text, text)
        check("统计数字", "42 条" in text and "40 条" in text and "1 次" in text, text)
    finally:
        with_color(old)

    print("\n[5] 事件行")
    old = with_color(False)
    try:
        line = ui.event("msg", "张三：你好")
        check("含时间戳", re.match(r"^\d{2}:\d{2}:\d{2}", line) is not None, line)
        check("含类别", "消息" in line, line)
        check("含内容", "张三：你好" in line)
        for kind in ("ok", "warn", "err", "info", "msg"):
            check(f"{kind} 能渲染", "  " in ui.event(kind, "x"))
    finally:
        with_color(old)

    print("\n[6] 配色开关")
    old = with_color(True)
    try:
        coloured = ui.event("err", "炸了")
        check("开色时有转义", "\x1b[" in coloured, repr(coloured[:20]))
    finally:
        with_color(old)
    old = with_color(False)
    try:
        check("关色时无转义", "\x1b[" not in ui.event("err", "炸了"))
    finally:
        with_color(old)

    print("\n[7] 日志格式")
    old = with_color(False)
    try:
        fmt = ConsoleFormatter()
        rec = logging.LogRecord("bot.chat", logging.INFO, __file__, 1, "收到消息 %s", ("hi",), None)
        out = fmt.format(rec)
        check("有耗时前缀", re.match(r"^\d{2}:\d{2}:\d{2}", out) is not None, out)
        check("有中文类别", "对话" in out, out)
        check("消息被格式化", "收到消息 hi" in out, out)
        check("不含模块全名", "bot.chat" not in out, out)
        rec_w = logging.LogRecord("bot.llm", logging.WARNING, __file__, 1, "限流了", (), None)
        check("警告级别可见", "限流了" in fmt.format(rec_w))
        rec_x = logging.LogRecord("bot", logging.ERROR, __file__, 1, "崩了", (), None)
        check("错误图标", "✕" in fmt.format(rec_x), fmt.format(rec_x))
    finally:
        with_color(old)

    print("\n[8] 标题设置不报错")
    try:
        ui.set_title("qq-multi-model · 测试")
        check("set_title 正常返回", True)
    except Exception as exc:  # noqa: BLE001
        check("set_title 正常返回", False, repr(exc))

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
