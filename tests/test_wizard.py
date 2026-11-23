"""首次配置向导的测试：用脚本化的输入模拟用户按键。

    python tests/test_wizard.py

不需要真实终端、不发网络请求（Key 校验和 NapCat 都会被替换掉）。
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app import wizard  # noqa: E402
from app.config import load_config  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


class ScriptedInput:
    """按顺序吐答案，用完就报错，避免测试意外多问/少问。"""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)

    def __call__(self, prompt: str = "") -> str:
        if not self.answers:
            raise AssertionError(f"没有更多预设答案了，但向导还在问：{prompt!r}")
        return self.answers.pop(0)

    def assert_empty(self) -> None:
        assert not self.answers, f"还剩没被消费的答案: {self.answers}"


def run_wizard(answers: list[str], path: Path) -> tuple[bool, str]:
    scripted = ScriptedInput(answers)
    buf = io.StringIO()
    with mock.patch.object(builtins, "input", scripted), \
         mock.patch.object(wizard, "_setup_napcat", return_value=None), \
         contextlib.redirect_stdout(buf):
        started = wizard.run_setup(path)
    scripted.assert_empty()
    return started, buf.getvalue()


def test_onebot_and_start_declined() -> None:
    print("\n[1] OneBot 全默认")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        # 后端默认 / Key / 不验证 / 端口默认 / 令牌自动生成 / 不启动
        started, out = run_wizard(["", "", "sk-test-123", "n", "", "", "n"], path)
        check("向导返回“不启动”", started is False)
        check("配置文件已生成", path.exists())
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        check("backend = onebot", data["backend"] == "onebot", str(data.get("backend")))
        check("api_key 已写入", data["providers"]["deepseek"]["api_key"] == "sk-test-123")
        check("端口 6700", data["onebot"]["port"] == 6700, str(data.get("onebot", {}).get("port")))
        token = data["onebot"]["access_token"]
        check("令牌自动生成（32 位）", isinstance(token, str) and len(token) == 32, str(token))
        check("令牌只展示一次", out.count(token) <= 1, f"出现了 {out.count(token)} 次")
        summary = out.split("配置完成", 1)[-1]
        check("汇总区用打码显示", token not in summary, "末尾汇总把令牌原样打印了")

        cfg = load_config(str(path), verbose=False)
        check("生成的配置能正常加载", cfg.get("providers.deepseek.api_key") == "sk-test-123")
        check("加载时无缺失 Key 告警", not any("api_key" in w for w in cfg.warnings), str(cfg.warnings))


def test_qqofficial() -> None:
    print("\n[2] QQ 官方机器人")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        started, _ = run_wizard(["2", "", "sk-x", "n", "102000000", "s3cret", "y", "n"], path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        check("backend = qqofficial", data["backend"] == "qqofficial", str(data["backend"]))
        check("app_id 已写入", data["qqofficial"]["app_id"] == "102000000")
        check("app_secret 已写入", data["qqofficial"]["client_secret"] == "s3cret")
        check("沙箱开关生效", data["qqofficial"]["sandbox"] is True)
        check("不启动", started is False)


def test_start_accepted() -> None:
    print("\n[3] 配完直接启动")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        started, _ = run_wizard(["", "", "sk-abc", "n", "", "", "y"], path)
        check("向导返回“启动”", started is True)


def test_key_validation_mock() -> None:
    print("\n[4] Key 校验（本地假接口）")

    class Handler(BaseHTTPRequestHandler):
        ok = True

        def do_GET(self):  # noqa: N802
            self.send_response(200 if Handler.ok else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": []}).encode())

        def log_message(self, *args):  # noqa: A002
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        Handler.ok = True
        ok, reason = wizard._validate_provider(wizard.get_provider("deepseek"), "sk-ok", f"http://127.0.0.1:{port}")
        check("200 → 通过", ok is True, reason)
        Handler.ok = False
        ok, reason = wizard._validate_provider(wizard.get_provider("deepseek"), "sk-bad", f"http://127.0.0.1:{port}")
        check("401 → 不通过且提示清楚", ok is False and "401" in reason, reason)
    finally:
        server.shutdown()


def test_free_port() -> None:
    print("\n[5] 端口占用检测")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        picked = wizard._free_port(taken)
    check("占用时换一个端口", picked != taken, f"{picked} == {taken}")
    check("空闲时原样返回", wizard._free_port(1) == 1)


def test_mask() -> None:
    print("\n[6] 密钥打码")
    check("空值友好显示", wizard._mask("") == "（未填）")
    masked = wizard._mask("sk-1234567890abcdef", 6)
    check("保留前 6 位", masked.startswith("sk-123"))
    check("后半段被遮住", "4567890" not in masked, masked)


def main() -> int:
    test_onebot_and_start_declined()
    test_qqofficial()
    test_start_accepted()
    test_key_validation_mock()
    test_free_port()
    test_mask()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
