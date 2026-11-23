"""NapCat 自动获取：查最新版本 / 选资产 / 下载 / 校验 / 解压 / 探测已安装。

NapCat 是第三方 QQ 协议端（GitHub: ``NapNeko/NapCatQQ``），首次使用需要把它的
release 压缩包下载到本地。下载第三方二进制必须守住三条底线，UI 层负责「先征得
用户同意」，本模块负责另外两条：

* **校验哈希** —— GitHub 新版 release 的资产带 ``digest`` 字段，下载后核对；
  老资产没有该字段时只记一条告警（由 UI 决定是否把风险提示给用户）。
* **拒绝路径穿越** —— 下载到本地时资产名只能是纯文件名；解压时拒绝 ``../``、
  绝对路径、带盘符等逃出目标目录的 zip 条目（zip-slip）。

本模块只提供可调用函数：**不做任何交互、不打印菜单、不读 input()**，所有可预期
失败统一抛 :class:`NapCatError`（message 为中文，可直接展示给用户）。

典型用法（由向导代码驱动）::

    installer = NapCatInstaller(home)
    existing = installer.find_existing(home)
    if existing is None:
        release = installer.fetch_latest()
        asset = installer.pick_asset(release)
        archive = installer.download(asset, home / "download", progress)
        installer.verify(archive, asset.get("digest"))
        root = installer.extract(archive, home)

公开 API 与签名见下方 :class:`NapCatInstaller`。仅依赖标准库。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("bot.napcat")

#: 访问 GitHub 时使用的 User-Agent（GitHub API 要求必须带）。
USER_AGENT = "qq-multi-model-napcat-installer/1.0"
#: GitHub API 推荐的 Accept 头。
_API_ACCEPT = "application/vnd.github+json"

_DOWNLOAD_CHUNK = 64 * 1024
_HASH_CHUNK = 1024 * 1024

#: 已安装目录里可能出现的可执行入口（统一小写比较）。
_ENTRY_NAMES = frozenset(
    {
        "napcatwinbootmain.exe",
        "launcher.bat",
        "launcher.sh",
        "launcher.command",
        "napcat.shell",
        "napcat.sh",
        "napcat.bat",
        "napcat.exe",
        "start.bat",
        "start.sh",
    }
)

#: 递归探测时跳过的大目录，避免在用户主目录里扫到天荒地老。
_SKIP_DIRS = frozenset(
    {"__pycache__", ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "site-packages"}
)
_MAX_DEPTH = 6

#: 平台关键字（按「分词」比较，避免 darwin 里含 win 的误判）。
_PLATFORM_TOKENS: dict[str, frozenset[str]] = {
    "win32": frozenset({"win", "windows", "win32", "win64", "winx64", "win64x"}),
    "darwin": frozenset({"mac", "macos", "macosx", "osx", "darwin"}),
    "linux": frozenset({"linux", "linux64", "linuxarm64"}),
}

_PLATFORM_LABELS = {"win32": "Windows", "darwin": "macOS", "linux": "Linux"}

_RELEASE_DIGEST_RE = re.compile(r"^[0-9a-fA-F]+$")
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


class NapCatError(Exception):
    """NapCat 下载流程中的可预期错误。

    ``str(exc)`` 是给用户看的中文说明，UI 层可以直接展示，不需要再包一层。
    """


def _platform_key() -> str | None:
    """把 ``sys.platform`` 归一到 :data:`_PLATFORM_TOKENS` 的键。"""
    plat = sys.platform.lower()
    if plat.startswith("win") or plat in {"cygwin", "msys"}:
        return "win32"
    if plat == "darwin":
        return "darwin"
    if plat.startswith("linux"):
        return "linux"
    return None


def _asset_tokens(name: str) -> set[str]:
    return {token for token in _TOKEN_SPLIT_RE.split(name.lower()) if token}


def _score_asset(name: str, platform: str) -> int:
    """给候选资产打分：本平台关键字加分，别的平台关键字减分。

    分值为正才认为「适用于当前平台」；这样 ``NapCat.Shell.MacOS.zip`` 在
    Windows 上会被排除，不会被误选。
    """
    tokens = _asset_tokens(name)
    own = _PLATFORM_TOKENS.get(platform, frozenset())
    score = 0
    if tokens & own:
        score += 50
    foreign: set[str] = set()
    for key, hints in _PLATFORM_TOKENS.items():
        if key != platform:
            foreign |= hints
    if tokens & foreign:
        score -= 50
    low = name.lower()
    if "shell" in low:
        score += 30
    if "napcat" in low:
        score += 10
    return score


def _safe_asset_name(name: str) -> str:
    """资产名只能是纯文件名，防止 ``../x``、``C:\\x`` 之类的串到别的目录。"""
    cleaned = name.strip()
    bad_chars = set('/\\:*?"<>|')
    if (
        not cleaned
        or cleaned in {".", ".."}
        or any(ch in bad_chars for ch in cleaned)
        or any(ord(ch) < 32 for ch in cleaned)
    ):
        raise NapCatError(f"下载 NapCat 失败：资产名不合法（{name!r}）")
    return cleaned


def _safe_dir_name(name: str) -> str:
    """由压缩包名推导解压出的目录名，去掉 Windows 非法字符并拒绝 ``..``。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip()).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise NapCatError(f"解压 NapCat 失败：压缩包名不合法（{name!r}），无法确定解压目录")
    return cleaned


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # 清不掉也不能盖掉真正的错误
        pass


def _ssl_context() -> ssl.SSLContext | None:
    """复用 app.net 的证书配置（macOS / 打包后必需），拿不到就返回 None。"""
    try:
        from .net import make_ssl_context
    except ImportError:
        return None
    return make_ssl_context()


def _open(request: urllib.request.Request, timeout: float):
    """按 URL 协议补上 TLS 上下文再打开；不是 https 就保持默认行为。"""
    kwargs: dict = {"timeout": timeout}
    if request.full_url.lower().startswith("https"):
        ctx = _ssl_context()
        if ctx is not None:
            kwargs["context"] = ctx
    return urllib.request.urlopen(request, **kwargs)  # noqa: S310


class NapCatInstaller:
    """NapCat 下载器。所有方法只做机制，不负责征得用户同意。

    :param home: 本项目的 NapCat 安装根目录（由调用方决定，通常是 ``<项目>/napcat``）。
    :param api_base: GitHub API 根地址，测试时可指向本地假服务。
    :param repo: release 所在仓库，``owner/name`` 形式。
    :param timeout: 单次 HTTP 请求超时（秒），同时用于连接和读取。
    """

    def __init__(
        self,
        home: Path,
        api_base: str = "https://api.github.com",
        repo: str = "NapNeko/NapCatQQ",
        timeout: float = 30.0,
    ) -> None:
        self.home = Path(home)
        self.api_base = str(api_base).strip().rstrip("/")
        self.repo = str(repo).strip().strip("/")
        self.timeout = float(timeout)

    # ------------------------------------------------------------------ 探测

    @staticmethod
    def find_existing(home: Path) -> Path | None:
        """在 ``home`` 下递归找已安装的 NapCat，返回安装根目录；没有返回 None。

        识别 ``NapCatWinBootMain.exe``、``launcher.bat``、``NapCat.Shell``、
        ``napcat.sh`` 等入口。入口是文件时返回它所在目录；入口本身是目录
        （解压出来的 ``NapCat.Shell`` 包根）时返回该目录。任何异常都吞掉并
        返回 None —— 探测失败等价于「没装」。
        """
        try:
            base = Path(home).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        if not base.is_dir():
            return None

        try:
            base_depth = len(base.parts)
            for root, dirs, files in os.walk(base):
                root_path = Path(root)
                if len(root_path.parts) - base_depth >= _MAX_DEPTH:
                    dirs[:] = []
                    continue
                dirs[:] = sorted(d for d in dirs if d.lower() not in _SKIP_DIRS)
                for name in sorted(files) + sorted(dirs):
                    if name.lower() not in _ENTRY_NAMES:
                        continue
                    candidate = root_path / name
                    if candidate.is_dir():
                        return candidate
                    return root_path
        except OSError:
            return None
        return None

    # ------------------------------------------------------------------ 查询

    def fetch_latest(self) -> dict:
        """GET GitHub 的 ``releases/latest``，返回裁剪后的 release 信息。

        返回形如::

            {"tag": "v4.x.x", "name": "...", "assets": [
                {"name": "NapCat.Shell.zip", "size": 123,
                 "browser_download_url": "https://...", "digest": "sha256:..."},
            ]}

        ``digest`` 在 GitHub 老资产上可能为 None。非 200、网络失败或响应不是
        合法 JSON 都抛 :class:`NapCatError`。
        """
        url = f"{self.api_base}/repos/{self.repo}/releases/latest"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": _API_ACCEPT},
        )
        try:
            with _open(request, self.timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            except Exception:  # noqa: BLE001 - 关响应失败不影响报错
                pass
            raise NapCatError(
                f"查询 NapCat 最新版本失败：GitHub 返回 HTTP {exc.code}（{url}）"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NapCatError(f"查询 NapCat 最新版本失败：网络不可用（{exc}）") from exc

        if status != 200:
            raise NapCatError(f"查询 NapCat 最新版本失败：GitHub 返回 HTTP {status}")

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NapCatError("查询 NapCat 最新版本失败：GitHub 返回的不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise NapCatError("查询 NapCat 最新版本失败：GitHub 返回的数据格式不对")

        tag = data.get("tag_name") or data.get("name")
        if not tag:
            raise NapCatError("查询 NapCat 最新版本失败：响应里缺少版本号（tag_name）")

        raw_assets = data.get("assets")
        if not isinstance(raw_assets, list):
            raise NapCatError("查询 NapCat 最新版本失败：响应里缺少 assets 列表")

        assets: list[dict] = []
        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            digest = item.get("digest")
            assets.append(
                {
                    "name": str(name),
                    "size": size,
                    "browser_download_url": str(item.get("browser_download_url") or ""),
                    "digest": str(digest) if digest else None,
                }
            )

        return {"tag": str(tag), "name": str(data.get("name") or tag), "assets": assets}

    def pick_asset(self, release: dict) -> dict:
        """按当前平台从 ``release["assets"]`` 里挑合适的 zip 资产。

        Windows 优先名字含 ``win`` / ``shell`` 的 zip，macOS 优先含
        ``mac`` / ``darwin`` 的 zip；别的平台的资产会被排到后面。挑不到抛
        :class:`NapCatError`，message 里列出全部可用资产名。
        """
        release = release if isinstance(release, dict) else {}
        assets = [item for item in (release.get("assets") or []) if isinstance(item, dict)]
        names = [str(item.get("name") or "") for item in assets]

        platform = _platform_key()
        if platform is None:
            raise NapCatError(
                f"暂不支持在 {sys.platform} 上自动下载 NapCat，可用资产："
                f"{'、'.join(n for n in names if n) or '（无）'}"
            )
        label = _PLATFORM_LABELS.get(platform, platform)

        best: dict | None = None
        best_score = 0
        for item in assets:
            name = str(item.get("name") or "")
            if not name.lower().endswith(".zip"):
                continue
            score = _score_asset(name, platform)
            if score > best_score:
                best, best_score = item, score

        if best is None:
            available = "、".join(n for n in names if n) or "（无）"
            raise NapCatError(
                f"在 release {release.get('tag') or release.get('name') or '最新版'} 里"
                f"找不到适用于{label}的 zip 资产，可用资产：{available}"
            )
        log.info("为%s选中 NapCat 资产：%s", label, best.get("name"))
        return best

    # ------------------------------------------------------------------ 下载

    def download(
        self,
        asset: dict,
        dest_dir: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """流式下载 ``asset`` 到 ``dest_dir/<asset.name>``，返回本地路径。

        ``progress(已下载字节, 总字节)`` 在每个数据块之后调用；总字节未知时
        第二个参数传 0。下载失败会删掉半截文件并抛 :class:`NapCatError`。
        先写 ``<name>.part``，全部收完才原子重命名，避免留下“看起来完整”的
        坏文件。
        """
        asset = asset if isinstance(asset, dict) else {}
        name = _safe_asset_name(str(asset.get("name") or ""))
        url = str(asset.get("browser_download_url") or "").strip()
        if not url:
            raise NapCatError(f"下载 NapCat 失败：资产 {name} 缺少下载地址")

        dest_dir = Path(dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NapCatError(f"下载 NapCat 失败：无法创建目录 {dest_dir}（{exc}）") from exc

        dest = dest_dir / name
        part = dest_dir / f"{name}.part"
        try:
            total = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            total = 0
        if total < 0:
            total = 0

        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
        )
        downloaded = 0
        ok = False
        try:
            try:
                response = _open(request, self.timeout)
            except urllib.error.HTTPError as exc:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
                raise NapCatError(f"下载 NapCat 失败：下载服务器返回 HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise NapCatError(f"下载 NapCat 失败：连不上下载服务器（{exc}）") from exc

            with response:
                status = getattr(response, "status", None) or response.getcode()
                if status != 200:
                    raise NapCatError(f"下载 NapCat 失败：下载服务器返回 HTTP {status}")
                if total <= 0:
                    # 资产没报大小就退而求其次看 Content-Length；都没有就按未知处理。
                    header = ""
                    headers = getattr(response, "headers", None)
                    if headers is not None:
                        header = headers.get("Content-Length") or ""
                    try:
                        total = int(header) if header else 0
                    except (TypeError, ValueError):
                        total = 0
                    if total < 0:
                        total = 0
                if progress is not None:
                    progress(0, total)
                with open(part, "wb") as handle:
                    while True:
                        chunk = response.read(_DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if progress is not None:
                            progress(downloaded, total)

            if total > 0 and downloaded != total:
                raise NapCatError(
                    f"下载 NapCat 失败：文件不完整（期望 {total} 字节，实际 {downloaded} 字节）"
                )
            os.replace(part, dest)
            ok = True
        except NapCatError:
            raise
        except OSError as exc:
            raise NapCatError(f"下载 NapCat 失败：没有写权限或磁盘出错（{exc}）") from exc
        except Exception as exc:  # noqa: BLE001 - 进度回调抛错之类也要收尾
            raise NapCatError(f"下载 NapCat 失败：{exc}") from exc
        finally:
            if not ok:
                _remove_file(part)
        log.info("NapCat 已下载：%s（%d 字节）", dest, downloaded)
        return dest

    # ------------------------------------------------------------------ 校验

    def verify(self, path: Path, digest: str | None) -> None:
        """核对文件哈希；不匹配抛 :class:`NapCatError`，message 含期望值和实际值。

        ``digest`` 形如 ``sha256:abcdef...``；GitHub 老资产没有该字段时传 None，
        此时只记一条 ``bot.napcat`` 告警，不抛异常。
        """
        path = Path(path)
        if digest is None or (isinstance(digest, str) and not digest.strip()):
            log.warning("NapCat 资产没有提供哈希（digest），跳过完整性校验：%s", path.name)
            return
        if not isinstance(digest, str):
            raise NapCatError(f"校验 NapCat 失败：无法识别的哈希格式（{digest!r}）")

        text = digest.strip()
        if ":" in text:
            algorithm, _, expected = text.partition(":")
            algorithm = algorithm.strip().lower()
            expected = expected.strip().lower()
        else:
            algorithm, expected = "sha256", text.lower()

        if not expected or not _RELEASE_DIGEST_RE.match(expected):
            raise NapCatError(f"校验 NapCat 失败：哈希值格式不对（{digest!r}）")
        try:
            hasher = hashlib.new(algorithm)
        except ValueError as exc:
            raise NapCatError(f"校验 NapCat 失败：不支持的哈希算法 {algorithm!r}") from exc

        if not path.is_file():
            raise NapCatError(f"校验 NapCat 失败：找不到下载好的文件（{path}）")
        try:
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(_HASH_CHUNK), b""):
                    hasher.update(block)
        except OSError as exc:
            raise NapCatError(f"校验 NapCat 失败：读不了文件（{exc}）") from exc

        actual = hasher.hexdigest()
        if actual != expected:
            raise NapCatError(
                f"校验 NapCat 失败：哈希不匹配，期望 {algorithm}:{expected}，"
                f"实际 {algorithm}:{actual}"
            )
        log.info("NapCat 哈希校验通过：%s", path.name)

    # ------------------------------------------------------------------ 解压

    def extract(self, zip_path: Path, dest_dir: Path) -> Path:
        """把 ``zip_path`` 解压到 ``dest_dir/<压缩包名去 .zip>``，返回解压根目录。

        逐条解析 zip 成员并做 zip-slip 检查：解析后的绝对路径必须仍在解压根
        目录内，否则抛 :class:`NapCatError`，不做任何越界写入。目标目录已存在
        时先整体删除；解压中途失败也会清掉半成品。
        """
        zip_path = Path(zip_path)
        if not zip_path.is_file():
            raise NapCatError(f"解压 NapCat 失败：找不到压缩包（{zip_path}）")

        stem = zip_path.name
        if stem.lower().endswith(".zip"):
            stem = stem[: -len(".zip")]
        root_name = _safe_dir_name(stem)

        try:
            base = Path(dest_dir).expanduser().resolve()
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NapCatError(f"解压 NapCat 失败：无法创建目标目录（{exc}）") from exc
        target = base / root_name

        if target.exists():
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except OSError as exc:
                raise NapCatError(
                    f"解压 NapCat 失败：无法清理旧目录 {target}（{exc}）"
                ) from exc
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NapCatError(f"解压 NapCat 失败：无法创建目录 {target}（{exc}）") from exc

        try:
            archive = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise NapCatError(f"解压 NapCat 失败：不是有效的 zip 文件（{exc}）") from exc

        try:
            with archive:
                for info in archive.infolist():
                    member_path = _resolve_member_path(target, info.filename)
                    if info.is_dir():
                        member_path.mkdir(parents=True, exist_ok=True)
                        continue
                    member_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as source, open(member_path, "wb") as sink:
                        shutil.copyfileobj(source, sink, _HASH_CHUNK)
        except NapCatError:
            shutil.rmtree(target, ignore_errors=True)
            raise
        except Exception as exc:  # noqa: BLE001 - zip 损坏 / 加密 / 压缩算法不支持等
            shutil.rmtree(target, ignore_errors=True)
            raise NapCatError(f"解压 NapCat 失败：{exc}") from exc

        log.info("NapCat 已解压到：%s", target)
        return target


def _resolve_member_path(root: Path, member_name: str) -> Path:
    """把 zip 里的成员名解析成安全路径，逃出 ``root`` 就抛 :class:`NapCatError`。"""
    if not member_name:
        raise NapCatError("解压 NapCat 失败：压缩包里有一个空的文件名，已拒绝解压")
    # zip 规范用 /，但恶意包可能塞反斜杠；统一后交给 abspath 归一化 ..，
    # 再确认结果仍在 root 之内（同时挡住绝对路径和带盘符的路径）。
    cleaned = member_name.replace("\\", "/")
    candidate = Path(os.path.abspath(os.path.join(root, cleaned)))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise NapCatError(
            f"解压 NapCat 失败：压缩包里含有危险的路径（{member_name}），已拒绝解压"
        ) from exc
    return candidate
