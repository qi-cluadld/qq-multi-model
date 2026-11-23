"""NapCat 下载器自测：只用标准库，直接 ``python tests/test_napcat.py`` 跑。

起一个本地 ``http.server`` 线程同时当假 GitHub API 和假下载源，不联网。
覆盖：正常全流程（fetch → pick → download → verify → extract）、哈希不匹配、
HTTP 404 / 非 JSON、digest 为 None、zip-slip 拒绝、find_existing 探测。
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.napcat import NapCatError, NapCatInstaller  # noqa: E402 - 要先补 sys.path

#: 假 release 里主资产的名字；解压根目录名由它去 .zip 得到。
ZIP_ASSET = "NapCat.Shell.zip"
MAC_ASSET = "NapCat.Shell.MacOS.zip"
TARBALL_ASSET = "NapCat.Shell.tar.gz"


def build_zip(extra: dict[str, bytes] | None = None) -> bytes:
    """造一个结构类似 NapCat.Shell.zip 的压缩包（入口文件放在根目录）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("launcher.bat", "@echo off\r\nstart NapCatWinBootMain.exe\r\n")
        archive.writestr("NapCatWinBootMain.exe", b"MZ" + b"\x00" * 64)
        archive.writestr("napcat/config/onebot.json", b'{"httpServers": []}')
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeGitHub:
    """本地假 GitHub：``/repos/.../releases/latest`` + ``/downloads/<asset>``。"""

    def __init__(self) -> None:
        self.state: dict = {"mode": "ok", "download_mode": "ok", "zip": build_zip()}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 静音，别把测试输出搞乱
                return

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
                owner.handle(self, self.path)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    # -- 生命周期 -----------------------------------------------------------
    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # -- 路由 ---------------------------------------------------------------
    def handle(self, handler: http.server.BaseHTTPRequestHandler, path: str) -> None:
        route = path.split("?", 1)[0]
        if route == "/repos/NapNeko/NapCatQQ/releases/latest":
            mode = self.state.get("mode", "ok")
            if mode == "404":
                self._send(handler, 404, b'{"message": "Not Found"}', "application/json")
            elif mode == "badjson":
                self._send(handler, 200, b"<html>rate limited?</html>", "text/html")
            else:
                payload = self.release_payload()
                self._send(handler, 200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        if route.startswith("/downloads/"):
            if self.state.get("download_mode") == "404":
                self._send(handler, 404, b"gone", "text/plain")
                return
            data = self.state["zip"]
            self._send(handler, 200, data, "application/zip")
            return
        self._send(handler, 404, b"not found", "text/plain")

    def release_payload(self) -> dict:
        data = self.state["zip"]
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        base = self.base_url

        def asset(name: str) -> dict:
            return {
                "name": name,
                "size": len(data),
                "browser_download_url": f"{base}/downloads/{name}",
                "digest": digest,
            }

        if self.state.get("mode") == "nozip":
            assets = [
                asset(TARBALL_ASSET),
                {
                    "name": "README.md",
                    "size": 10,
                    "browser_download_url": f"{base}/downloads/README.md",
                    "digest": None,
                },
            ]
        else:
            assets = [asset(ZIP_ASSET), asset(MAC_ASSET), asset(TARBALL_ASSET)]
        return {"tag_name": "v9.9.9-test", "name": "NapCat v9.9.9-test", "assets": assets}

    @staticmethod
    def _send(handler, status: int, body: bytes, content_type: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class NapCatInstallerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fake = FakeGitHub()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fake.close()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="napcat-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake.state = {"mode": "ok", "download_mode": "ok", "zip": build_zip()}
        self.installer = NapCatInstaller(self.tmp / "home", api_base=self.fake.base_url)

    def install_dir(self) -> Path:
        return self.tmp / "install"

    # -- 1. 正常全流程 ------------------------------------------------------
    def test_full_flow(self) -> None:
        release = self.installer.fetch_latest()
        self.assertEqual(release["tag"], "v9.9.9-test")
        self.assertEqual(release["name"], "NapCat v9.9.9-test")
        self.assertTrue(release["assets"])
        self.assertTrue(all({"name", "size", "browser_download_url", "digest"} <= set(a) for a in release["assets"]))

        asset = self.installer.pick_asset(release)
        self.assertTrue(asset["name"].lower().endswith(".zip"), asset["name"])
        self.assertIn(asset["name"], [a["name"] for a in release["assets"]])

        progress: list[tuple[int, int]] = []
        downloaded = self.installer.download(
            asset, self.tmp / "download", progress=lambda done, total: progress.append((done, total))
        )
        self.assertTrue(downloaded.is_file())
        self.assertEqual(downloaded.name, asset["name"])
        self.assertEqual(downloaded.read_bytes(), self.fake.state["zip"])
        self.assertEqual(progress[-1], (len(self.fake.state["zip"]), len(self.fake.state["zip"])))

        self.installer.verify(downloaded, asset["digest"])

        root = self.installer.extract(downloaded, self.install_dir())
        self.assertTrue(root.is_dir())
        self.assertTrue((root / "launcher.bat").is_file())
        self.assertTrue((root / "NapCatWinBootMain.exe").is_file())
        self.assertTrue((root / "napcat" / "config" / "onebot.json").is_file())
        expected_root = self.install_dir().resolve() / Path(asset["name"]).stem
        self.assertEqual(root, expected_root)

        # find_existing 能在解压结果里认出安装目录
        self.assertEqual(NapCatInstaller.find_existing(self.install_dir()), root)

        # 重复解压应覆盖旧目录，而不是报错
        root_again = self.installer.extract(downloaded, self.install_dir())
        self.assertEqual(root_again, root)
        self.assertTrue((root / "launcher.bat").is_file())

    # -- 2. 哈希不匹配 ------------------------------------------------------
    def test_hash_mismatch_raises(self) -> None:
        release = self.installer.fetch_latest()
        asset = self.installer.pick_asset(release)
        downloaded = self.installer.download(asset, self.tmp / "download")

        wrong = "sha256:" + "0" * 64
        with self.assertRaises(NapCatError) as caught:
            self.installer.verify(downloaded, wrong)
        message = str(caught.exception)
        self.assertIn("期望", message)
        self.assertIn("实际", message)
        self.assertIn("0" * 64, message)
        self.assertIn(hashlib.sha256(downloaded.read_bytes()).hexdigest(), message)

    # -- 3. 404 / 非 JSON ---------------------------------------------------
    def test_fetch_404_raises(self) -> None:
        self.fake.state["mode"] = "404"
        with self.assertRaises(NapCatError) as caught:
            self.installer.fetch_latest()
        self.assertIn("404", str(caught.exception))

    def test_fetch_non_json_raises(self) -> None:
        self.fake.state["mode"] = "badjson"
        with self.assertRaises(NapCatError) as caught:
            self.installer.fetch_latest()
        self.assertIn("JSON", str(caught.exception))

    # -- 4. digest 为 None 不抛异常 -----------------------------------------
    def test_digest_none_only_warns(self) -> None:
        release = self.installer.fetch_latest()
        asset = dict(self.installer.pick_asset(release), digest=None)
        downloaded = self.installer.download(asset, self.tmp / "download")
        with self.assertLogs("bot.napcat", level="WARNING") as logs:
            self.assertIsNone(self.installer.verify(downloaded, None))
        self.assertTrue(any("哈希" in line for line in logs.output), logs.output)

    # -- 5. zip-slip 拒绝 ---------------------------------------------------
    def test_zip_slip_rejected(self) -> None:
        self.fake.state["zip"] = build_zip({"../evil.txt": b"pwned"})
        release = self.installer.fetch_latest()
        asset = self.installer.pick_asset(release)
        archive = self.installer.download(asset, self.tmp / "download")

        with self.assertRaises(NapCatError) as caught:
            self.installer.extract(archive, self.install_dir())
        self.assertIn("evil.txt", str(caught.exception))
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertFalse((self.install_dir() / "evil.txt").exists())

    # -- 6. find_existing ---------------------------------------------------
    def test_find_existing(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIsNone(NapCatInstaller.find_existing(empty))
        self.assertIsNone(NapCatInstaller.find_existing(self.tmp / "missing"))
        self.assertIsNone(NapCatInstaller.find_existing(self.tmp / "launcher.bat"))  # 是文件不是目录

        target = self.tmp / "nested" / "deep"
        target.mkdir(parents=True)
        (target / "NapCatWinBootMain.exe").write_bytes(b"MZ")
        self.assertEqual(NapCatInstaller.find_existing(self.tmp / "nested"), target.resolve())

        direct = self.tmp / "direct"
        direct.mkdir()
        (direct / "launcher.bat").write_text("@echo off", encoding="utf-8")
        self.assertEqual(NapCatInstaller.find_existing(direct), direct.resolve())

    # -- 附加：挑不到资产要列出可用资产名 -----------------------------------
    def test_pick_asset_without_zip_lists_names(self) -> None:
        self.fake.state["mode"] = "nozip"
        release = self.installer.fetch_latest()
        with self.assertRaises(NapCatError) as caught:
            self.installer.pick_asset(release)
        message = str(caught.exception)
        self.assertIn(TARBALL_ASSET, message)
        self.assertIn("README.md", message)

    # -- 附加：下载失败要减掉半截文件 ---------------------------------------
    def test_download_failure_cleans_partial_file(self) -> None:
        self.fake.state["download_mode"] = "404"
        release = self.installer.fetch_latest()
        asset = self.installer.pick_asset(release)
        download_dir = self.tmp / "download"
        with self.assertRaises(NapCatError) as caught:
            self.installer.download(asset, download_dir)
        self.assertIn("404", str(caught.exception))
        self.assertEqual(list(download_dir.iterdir()), [])


def main() -> int:
    print(f"Python {sys.version.split()[0]} · 本地假 GitHub 测试 NapCat 下载器")
    suite = unittest.TestLoader().loadTestsFromTestCase(NapCatInstallerTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"结果：共 {result.testsRun} 个用例，失败 {len(result.failures)}，错误 {len(result.errors)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
