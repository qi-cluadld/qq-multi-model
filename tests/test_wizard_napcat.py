"""向导 + NapCat 下载模块的集成测试。

    python tests/test_wizard_napcat.py

起一个本地假 GitHub（release 接口 + 下载源），让向导真的走一遍
「查版本 → 选资产 → 下载 → 校验 → 解压」，最后检查配置和文件。
"""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app import napcat, wizard  # noqa: E402

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
    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, prompt: str = "") -> str:
        if not self.answers:
            raise AssertionError(f"没有更多预设答案了，但向导还在问：{prompt!r}")
        return self.answers.pop(0)


def make_napcat_zip() -> bytes:
    """造一个伪 NapCat 压缩包（带入口文件）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("NapCat.Shell/NapCatWinBootMain.exe", b"MZ fake exe")
        zf.writestr("NapCat.Shell/launcher.bat", b"@echo off\r\necho hi\r\n")
    return buf.getvalue()


class FakeGitHub:
    def __init__(self, payload: bytes, digest: str | None):
        self.payload = payload
        self.digest = digest
        self.port = 0
        self.download_hits = 0

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.endswith("/releases/latest"):
                    body = json.dumps({
                        "tag_name": "v9.9.9-test",
                        "name": "test release",
                        "assets": [{
                            "name": "NapCat.Shell.zip",
                            "size": len(outer.payload),
                            "browser_download_url":
                                f"http://127.0.0.1:{outer.port}/dl/NapCat.Shell.zip",
                            "digest": outer.digest,
                        }],
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.startswith("/dl/"):
                    outer.download_hits += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(outer.payload)))
                    self.end_headers()
                    self.wfile.write(outer.payload)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):  # noqa: A002
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()


def patched_installer(port: int):
    original = napcat.NapCatInstaller

    class Bound(original):  # type: ignore[misc, valid-type]
        def __init__(self, home, **kwargs):
            kwargs.setdefault("api_base", f"http://127.0.0.1:{port}")
            super().__init__(home, **kwargs)

    return Bound


def run_with(tmp: Path, port: int, answers: list[str]) -> tuple[bool, str]:
    cfg_path = tmp / "config.yaml"
    scripted = ScriptedInput(answers)
    buf = io.StringIO()
    with mock.patch.object(builtins, "input", scripted), \
         mock.patch.object(wizard, "app_dir", return_value=tmp), \
         mock.patch.object(napcat, "NapCatInstaller", patched_installer(port)), \
         contextlib.redirect_stdout(buf):
        started = wizard.run_setup(cfg_path)
    return started, buf.getvalue()


def test_download_success() -> None:
    print("\n[1] 下载 → 校验 → 解压 全流程")
    payload = make_napcat_zip()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    gh = FakeGitHub(payload, digest)
    gh.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 后端默认 / Key / 不验证 / 端口默认 / 令牌手填 / 下载 / 确认 / 不启动
            started, out = run_with(tmp, gh.port, ["", "", "sk-test", "n", "", "tok123", "", "y", "n"])

            cfg = yaml.safe_load((tmp / "config.yaml").read_text(encoding="utf-8"))
            check("配置已写入", cfg["providers"]["deepseek"]["api_key"] == "sk-test")
            check("令牌用用户填的", cfg["onebot"]["access_token"] == "tok123")

            exe = list((tmp / "napcat").rglob("NapCatWinBootMain.exe"))
            check("入口文件存在", bool(exe), "没解出启动器")
            check("解压目录已压平（不再套一层同名目录）", bool(exe) and not (exe[0].parent / "NapCat.Shell").exists(),
                  "还有多余的一层")
            check("引导里指向含启动器的目录", bool(exe) and exe[0].parent.as_posix() in out.replace("\\", "/"),
                  "引导里的路径不对")
            check("下载源被访问一次", gh.download_hits == 1, str(gh.download_hits))
            check("压缩包已清理", not (tmp / "napcat" / "NapCat.Shell.zip").exists())

            check("引导里给出了 ws 地址", f"ws://127.0.0.1:6700/onebot" in out, "没打印 ws 地址")
            check("引导里给出了令牌", "tok123" in out, "没打印令牌")
            check("未启动", started is False)
    finally:
        gh.stop()


def test_digest_mismatch() -> None:
    print("\n[2] 哈希不匹配 → 拒绝解压")
    payload = make_napcat_zip()
    wrong = "sha256:" + "0" * 64
    gh = FakeGitHub(payload, wrong)
    gh.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            started, out = run_with(tmp, gh.port, ["", "", "sk-test", "n", "", "tok123", "", "y", "n"])
            check("提示校验失败", "校验" in out or "哈希" in out, "没有提示")
            check("没有解压出目录", not (tmp / "napcat" / "NapCat.Shell").exists())
            check("配置仍然写好了", (tmp / "config.yaml").exists())
            check("未启动", started is False)
    finally:
        gh.stop()


def test_user_declines_download() -> None:
    print("\n[3] 用户拒绝下载 → 不联网")
    payload = make_napcat_zip()
    gh = FakeGitHub(payload, None)
    gh.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 下载选项里选「我自己去下载」(2)
            started, out = run_with(tmp, gh.port, ["", "", "sk-test", "n", "", "tok123", "2", "n"])
            check("没有发起下载", gh.download_hits == 0, f"下载了 {gh.download_hits} 次")
            check("给出了仓库地址", "github.com/NapNeko/NapCatQQ" in out, "没给链接")
            check("给出了 ws 配置指引", "ws://127.0.0.1:6700/onebot" in out)
            check("配置写好了", (tmp / "config.yaml").exists())
    finally:
        gh.stop()


def test_existing_detected() -> None:
    print("\n[4] 已装 NapCat → 直接复用，不下载")
    payload = make_napcat_zip()
    gh = FakeGitHub(payload, None)
    gh.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            installed = tmp / "napcat" / "NapCat.Shell"
            installed.mkdir(parents=True)
            (installed / "NapCatWinBootMain.exe").write_bytes(b"MZ")
            # 复用确认 "y"
            started, out = run_with(tmp, gh.port, ["", "", "sk-test", "n", "", "tok123", "y", "n"])
            check("识别到已安装", str(installed).replace("\\", "/") in out.replace("\\", "/"),
                  "没打印已有目录")
            check("没有重复下载", gh.download_hits == 0, f"下载了 {gh.download_hits} 次")
    finally:
        gh.stop()


def main() -> int:
    test_download_success()
    test_digest_mismatch()
    test_user_declines_download()
    test_existing_detected()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
