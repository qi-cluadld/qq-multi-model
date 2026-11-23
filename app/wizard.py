"""首次运行向导：不用手改 YAML，问几个问题就把 config.yaml 写好。

设计原则：
- 能按回车就用默认值，问题越少越好
- 任何网络动作（校验 Key、下载 NapCat）都先问，默认不下载
- 随时 Ctrl+C 可以退出，已填的内容不会写坏文件
"""

from __future__ import annotations

import getpass
import os
import secrets
import socket
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import yaml

from .config import DEFAULTS, app_dir
from .providers import get_provider, list_providers, model_spec_for

NAPCAT_REPO_PAGE = "https://github.com/NapNeko/NapCatQQ/releases"
DEEPSEEK_KEYS_PAGE = "https://platform.deepseek.com/api_keys"

BOX_W = 52


# ---------------------------------------------------------------- 输出辅助

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty() and os.name != "nt" or os.environ.get("WT_SESSION") is not None


_COLOR = _supports_color()


def _c(text: str, code: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _title(text: str) -> None:
    print()
    print(_c("─" * BOX_W, "90"))
    print(_c(f" {text}", "1;36"))
    print(_c("─" * BOX_W, "90"))


def _step(n: int, total: int, text: str) -> None:
    print()
    print(_c(f"[{n}/{total}] {text}", "1;33"))


def _hint(text: str) -> None:
    for line in text.splitlines():
        print(_c(f"      {line}", "90"))


def is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 提问

def ask(prompt: str, default: str = "", secret: bool = False, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            if secret:
                raw = getpass.getpass(f"  {prompt}{suffix}: ")
            else:
                raw = input(f"  {prompt}{suffix}: ")
        except EOFError:
            return default
        raw = raw.strip()
        if raw:
            return raw
        if default or allow_empty:
            return default
        print(_c("      这一项不能空，请再填一次（或按 Ctrl+C 退出）", "31"))


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"  {prompt} {hint}: ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes", "是", "1")


def ask_choice(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    keys = {k for k, _ in options}
    for i, (key, label) in enumerate(options, 1):
        mark = _c("→", "32") if key == default else " "
        print(f"   {mark} {i}) {label}")
    while True:
        try:
            raw = input(f"  {prompt} [默认 {list(keys).index(default) + 1}]: ").strip()
        except EOFError:
            return default
        if not raw:
            return default
        if raw in keys:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(_c("      请输入序号", "31"))


# ---------------------------------------------------------------- 小工具

def _free_port(preferred: int) -> int:
    def usable(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    if usable(preferred):
        return preferred
    for port in range(preferred + 1, preferred + 40):
        if usable(port):
            return port
    return preferred


def _probe_provider(provider, api_key: str, base_url: str = "",
                    timeout: float = 15) -> tuple[bool, str, list[str]]:
    """连一下服务商的 /models：既验证 Key，又拿回账号下真实可用的模型。

    返回 ``(是否成功, 说明, 模型 id 列表)``。失败时列表为空，调用方回退到内置清单。
    """
    import json

    base = (base_url or provider.base_url).rstrip("/")
    if provider.kind == "anthropic":
        url = base + "/v1/models"
        headers = {"anthropic-version": "2023-06-01", "User-Agent": "qq-multi-model"}
        if api_key:
            headers["x-api-key"] = api_key
    else:
        url = base + "/models"
        headers = {"User-Agent": "qq-multi-model"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, headers=headers)
    kwargs = {"timeout": timeout}
    if url.startswith("https"):
        from .net import make_ssl_context

        ctx = make_ssl_context()
        if ctx is not None:
            kwargs["context"] = ctx

    try:
        with urllib.request.urlopen(req, **kwargs) as resp:  # noqa: S310
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        mapping = {401: "Key 无效（401）", 402: "余额不足（402）", 403: "没有权限（403）",
                   404: "接口地址不对（404）"}
        return False, mapping.get(exc.code, f"HTTP {exc.code}"), []
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{type(exc).__name__}", []

    if status != 200:
        return False, f"HTTP {status}", []

    try:
        payload = json.loads(raw.decode("utf-8"))
        data = payload.get("data") or payload.get("models") or []
        models = sorted({str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")})
    except Exception:  # noqa: BLE001
        return True, "已连通（但模型列表解析失败，可手动填）", []
    return True, f"HTTP {status}", models


def _choose_model(provider, live_models: list[str]) -> str:
    """优先让用户从「账号下真实可用」的模型里挑。"""
    if not live_models:
        return provider.default_model or (provider.models[0] if provider.models else "")

    candidates = live_models[:20]
    default = provider.default_model if provider.default_model in candidates else candidates[0]
    print()
    print("  账号下可用模型：")
    for i, name in enumerate(candidates, 1):
        mark = "  ← 默认" if name == default else ""
        print(f"   {i:2d}) {name}{mark}")
    if len(live_models) > len(candidates):
        print(f"   …还有 {len(live_models) - len(candidates)} 个，启动后可用 /model 查看")

    while True:
        raw = ask("选序号，或直接输入模型名", default=default)
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        if raw:
            return raw


def _write_config(path: Path, data: dict) -> None:
    header = (
        "# ============================================================\n"
        "#  DeepSeek QQ 机器人 · 配置文件\n"
        "#  本文件由首次运行向导生成，想改可以重新运行：qq-multi-model --setup\n"
        "# ============================================================\n\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _mask(value: str, keep: int = 6) -> str:
    if not value:
        return "（未填）"
    return value[:keep] + "*" * max(0, min(20, len(value) - keep))


def _flatten_dir(root: Path, max_depth: int = 4) -> Path:
    """上游压缩包常常自带一层同名目录，这里压平，别让用户点进两层才找到启动器。"""
    current = root
    for _ in range(max_depth):
        try:
            entries = list(current.iterdir())
        except OSError:
            return current
        if len(entries) == 1 and entries[0].is_dir():
            current = entries[0]
        else:
            break
    return current


# ---------------------------------------------------------------- NapCat 引导

def _napcat_guide(install_dir: Path | None, port: int, token: str) -> None:
    where = str(install_dir) if install_dir else "（你自己安装的目录）"
    _hint(
        f"""NapCat 装好后还需要三步：

  1. 确认电脑上已经装了 QQ（NT 版），没有的话先去 im.qq.com 下载
  2. 启动 NapCat：进入 {where}
     运行里面的启动器（一般是 NapCatWinBootMain.exe 或 launcher.bat）
  3. 手机 QQ 扫码登录（建议用不常用的小号），登录后打开
     http://localhost:6099 进 WebUI → 网络配置 → 新建「WebSocket 服务器」

     地址填：  ws://127.0.0.1:{port}/onebot
     消息格式：array
     Token：   {token or '（留空，和配置保持一致即可）'}

  配好后回到这个窗口，看到「QQ 端已连接」就成功了。"""
    )


def _setup_napcat(port: int, token: str) -> Path | None:
    """返回 NapCat 目录；None 表示用户自行处理。"""
    try:
        from . import napcat as nc
    except ImportError:
        _hint("（当前版本未包含 NapCat 下载模块，请手动安装）")
        return None

    home = app_dir() / "napcat"
    try:
        existing = nc.NapCatInstaller.find_existing(home)
    except Exception:  # noqa: BLE001
        existing = None

    if existing:
        print()
        print(f"  已经找到 NapCat：{existing}")
        if ask_yes_no("  就用这个？", default=True):
            return existing

    print()
    print(_c("  没有检测到 NapCat。", "33"))
    _hint(
        """NapCat 是第三方开源的 QQ 协议端，负责让程序能收发 QQ 消息。
  它不是本项目的一部分，也可以自己去装。"""
    )
    choice = ask_choice(
        "  要怎么处理？",
        [
            ("download", "帮我从 GitHub 官方仓库下载（我会先确认再下）"),
            ("manual", "我自己去下载，给我个链接"),
            ("skip", "已经装好了 / 先跳过"),
        ],
        default="download",
    )

    if choice == "manual":
        try:
            webbrowser.open(NAPCAT_REPO_PAGE)
        except Exception:  # noqa: BLE001
            pass
        _hint(f"地址：{NAPCAT_REPO_PAGE}")
        _napcat_guide(None, port, token)
        return None
    if choice == "skip":
        _napcat_guide(None, port, token)
        return None

    installer = nc.NapCatInstaller(home=home)
    try:
        release = installer.fetch_latest()
        asset = installer.pick_asset(release)
    except nc.NapCatError as exc:
        print(_c(f"  查不到可下载的版本：{exc}", "31"))
        _hint(f"请手动下载：{NAPCAT_REPO_PAGE}")
        return None

    size_mb = (asset.get("size") or 0) / 1024 / 1024
    print()
    print(f"  版本：{release.get('tag')}")
    print(f"  文件：{asset.get('name')}（约 {size_mb:.1f} MB）")
    print(f"  来源：https://github.com/NapNeko/NapCatQQ/releases")
    print(f"  保存到：{home}")
    if not ask_yes_no("  现在下载？", default=True):
        _napcat_guide(None, port, token)
        return None

    def progress(done: int, total: int) -> None:
        if total:
            pct = done * 100 / total
            sys.stdout.write(f"\r      下载中 {pct:5.1f}%  ({done / 1048576:.1f}/{total / 1048576:.1f} MB)")
        else:
            sys.stdout.write(f"\r      下载中 {done / 1048576:.1f} MB")
        sys.stdout.flush()

    try:
        archive = installer.download(asset, home, progress=progress)
        print()
        installer.verify(archive, asset.get("digest"))
        target = _flatten_dir(installer.extract(archive, home))
        try:  # 解压完就把 zip 删掉，目录里只留可用的文件
            archive.unlink()
        except OSError:
            pass
    except nc.NapCatError as exc:
        print()
        print(_c(f"  下载或解压失败：{exc}", "31"))
        _hint(f"可以手动下载：{NAPCAT_REPO_PAGE}")
        return None
    except Exception as exc:  # noqa: BLE001
        print()
        print(_c(f"  出错了：{exc}", "31"))
        return None

    print(_c(f"  ✔ 已解压到：{target}", "32"))
    _napcat_guide(target, port, token)
    return target


# ---------------------------------------------------------------- 主流程

def run_setup(config_path: Path, total_steps: int = 4) -> bool:
    """跑一遍向导并写配置。返回 True 表示配完可以直接启动。"""
    _title("DeepSeek QQ 机器人 · 首次配置")
    _hint("按回车使用方括号里的默认值，Ctrl+C 随时退出。")

    data = yaml.safe_load(yaml.safe_dump(DEFAULTS))  # 深拷贝默认值

    # 1. 接入方式
    _step(1, total_steps, "选择用哪种方式接 QQ")
    backend = ask_choice(
        "  你的选择",
        [
            ("onebot", "NapCat / OneBot（推荐，用普通 QQ 号，群聊私聊都行）"),
            ("qqofficial", "QQ 官方机器人（要 appid + secret，群聊必须 @）"),
        ],
        default="onebot",
    )
    data["backend"] = backend

    # 2. 选模型服务商
    _step(2, total_steps, "选择模型服务商")
    options = [(p.name, f"{p.label}（默认 {p.default_model}）") for p in list_providers()]
    provider = get_provider(ask_choice("  用哪家的模型", options, default="deepseek"))
    data["defaults"]["model"] = model_spec_for(provider.name, provider.default_model)
    _hint(
        f"默认模型：{data['defaults']['model']}\n"
        f"聊天里可以用 /model 随时切换，不锁死在这一家。"
    )

    api_key = ""
    if provider.needs_key:
        _hint(f"到 {provider.label} 控制台申请 API Key，或直接设环境变量 {provider.api_key_env}")
        while not api_key:
            api_key = ask("API Key")
            if not api_key:
                print(_c("      没有 Key 模型没法回话，还是填一个吧。", "31"))
        data["providers"].setdefault(provider.name, {})["api_key"] = api_key
    else:
        _hint(f"{provider.label} 是本地服务，不需要 API Key；确认它已经在运行即可。")
        data["providers"].setdefault(provider.name, {})["base_url"] = provider.base_url

    custom_base = (data["providers"].get(provider.name) or {}).get("base_url", "")
    live_models: list[str] = []
    if provider.needs_key:
        if ask_yes_no("  现在连一下服务商，验证 Key 并拉取可用模型？", default=True):
            sys.stdout.write("      连接中…")
            sys.stdout.flush()
            ok, reason, live_models = _probe_provider(provider, api_key, custom_base)
            print("\r" + " " * 24 + "\r", end="")
            if ok:
                print(_c(f"      ✔ 连通，账号下有 {len(live_models)} 个模型（{reason}）", "32"))
            else:
                print(_c(f"      ✘ 没连上：{reason}", "33"))
                if not ask_yes_no("      仍然继续？", default=True):
                    return False
                live_models = []
    else:
        ok, reason, live_models = _probe_provider(provider, "", custom_base)
        if ok and live_models:
            print(_c(f"      检测到本地有 {len(live_models)} 个模型", "32"))
        else:
            print(_c(f"      没能连上 {provider.label}（{reason}），请确认它正在运行", "33"))

    chosen = _choose_model(provider, live_models)
    if not chosen:
        print(_c("      没有可用的模型名，请在 config.yaml 里手动填 defaults.model 后重试", "31"))
        return False
    data["defaults"]["model"] = model_spec_for(provider.name, chosen)

    # 3. 后端细节
    napcat_dir: Path | None = None
    if backend == "onebot":
        _step(3, total_steps, "配置本地端口和访问令牌")
        preferred = int(data["onebot"]["port"])
        port = _free_port(preferred)
        if port != preferred:
            print(_c(f"      端口 {preferred} 已被占用，改用 {port}", "33"))
        raw_port = ask("监听端口", default=str(port))
        try:
            port = int(raw_port)
        except ValueError:
            port = preferred
        token = ask("访问令牌（防止别人连你的机器人，回车自动生成）", default="", allow_empty=True)
        if not token:
            token = secrets.token_hex(16)
            print(_c(f"      已生成访问令牌：{token}", "90"))
            print(_c("      （等下配 NapCat 时要用这个，只显示这一次，也可在 config.yaml 里查到）", "90"))
        data["onebot"]["port"] = port
        data["onebot"]["access_token"] = token

        _step(4, total_steps, "准备 NapCat")
        napcat_dir = _setup_napcat(port, token)
    else:
        _step(3, total_steps, "填 QQ 开放平台的应用信息")
        _hint("去 https://q.qq.com 创建机器人 →「开发设置」里能拿到这两个值")
        data["qqofficial"]["app_id"] = ask("AppID")
        data["qqofficial"]["client_secret"] = ask("AppSecret")
        if ask_yes_no("  先用沙箱环境调试？", default=False):
            data["qqofficial"]["sandbox"] = True

    # 写配置
    _write_config(config_path, data)

    print()
    print(_c("─" * BOX_W, "90"))
    print(_c("  配置完成", "1;32"))
    print(f"  文件：{config_path}")
    print(f"  方式：{'NapCat / OneBot' if backend == 'onebot' else 'QQ 官方机器人'}")
    print(f"  模型：{data['defaults']['model']}")
    print(f"  Key ：{_mask(api_key) if api_key else '（本地服务，无需）'}")
    if backend == "onebot":
        print(f"  端口：{data['onebot']['port']}   令牌：{_mask(token, 8)}")
    print(_c("─" * BOX_W, "90"))

    if ask_yes_no("  现在就启动机器人？", default=True):
        return True

    print()
    if napcat_dir:
        _hint("装好 NapCat 并配好反向 WS 后，双击 start 脚本或重新运行本程序即可。")
    else:
        _hint("随时重新运行本程序即可启动；改配置用：qq-multi-model --setup")
    return False
