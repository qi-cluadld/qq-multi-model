"""多模型 LLM 客户端。

同一套 ``chat()`` 接口背后支持两类后端：

* ``kind="openai"``    —— OpenAI 兼容的 ``/chat/completions``（DeepSeek、OpenAI、
  Kimi、智谱、通义千问、Ollama、OpenRouter、SiliconFlow 等）
* ``kind="anthropic"`` —— Anthropic 原生 ``/v1/messages``（system 单独放顶层）

配置读取走 ``cfg.get("providers.<name>.api_key")`` 这样的点号路径，
没配置时再退回环境变量；base_url 同理（先配置，后内置默认值）。

内置模型清单只是推荐快照；``list_models()`` 会在运行时向服务商查询账号下
真实可用的模型，并按 provider 缓存 10 分钟。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import aiohttp

from .net import make_ssl_context
from .providers import (
    Provider,
    ProviderError,
    get_provider,
    list_providers,
    model_spec_for,
    resolve_model,
)

log = logging.getLogger("bot.llm")


class LLMError(Exception):
    """message 是给用户看的中文，可以直接回给用户。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# 429 和所有 5xx 都值得重试
_RETRY_STATUS = {429} | set(range(500, 600))

_OPENAI_SUFFIX = "/chat/completions"
_ANTHROPIC_SUFFIX = "/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# 运行时查询到的模型清单缓存 10 分钟；查询失败不缓存
_MODELS_CACHE_TTL = 600.0
_OPENAI_MODELS_SUFFIX = "/models"
_ANTHROPIC_MODELS_SUFFIX = "/v1/models"


class LLMClient:
    """一个可复用的多模型对话客户端。

    ``cfg`` 可以是 ``app.config.Config``，也可以是有 ``.get(path, default)``
    方法的任何对象（测试里用的假配置就是这样）。
    """

    def __init__(self, cfg, timeout: float = 60.0, retries: int = 2):
        self.cfg = cfg
        try:
            self.timeout = max(1.0, float(timeout))
        except (TypeError, ValueError):
            self.timeout = 60.0
        try:
            self.retries = max(0, int(retries))
        except (TypeError, ValueError):
            self.retries = 2
        # 重试退避的基准秒数：1.5、3.0、6.0…（测试里会调小）
        self.retry_backoff = 1.5
        self._session: aiohttp.ClientSession | None = None
        # 运行时查到的模型清单：provider.name -> (缓存时刻, 已排序的模型 id 列表)
        self._models_cache: dict[str, tuple[float, list[str]]] = {}

    # ------------------------------------------------------------ 对外 API

    async def chat(self, messages: list[dict[str, str]], model: str | None = None) -> str:
        """发送一轮对话，返回助手回复的纯文本。"""
        provider, model_name = self._resolve_model(model)
        api_key = self._api_key(provider)
        base_url = self._base_url(provider)
        temperature = self._temperature()
        max_tokens = self._max_tokens()

        if not isinstance(messages, list) or not messages:
            raise LLMError("没有可发送的消息")

        if provider.kind == "anthropic":
            url, payload, headers = self._anthropic_request(
                provider, base_url, api_key, model_name, messages, temperature, max_tokens
            )
        elif provider.kind == "openai":
            url, payload, headers = self._openai_request(
                provider, base_url, api_key, model_name, messages, temperature, max_tokens
            )
        else:
            raise LLMError(f"不支持的接口类型：{provider.kind}（{provider.label}）")

        log.info("→ %s [%s] %s", provider.label, model_name, _preview(messages))
        started = asyncio.get_running_loop().time()
        reply = await self._post(provider, url, payload, headers)
        elapsed = asyncio.get_running_loop().time() - started
        log.info("← %s %.1fs %s", provider.label, elapsed, _preview_text(reply))
        return reply

    async def close(self) -> None:
        """关闭内部复用的 HTTP 会话（可以重复调用）。"""
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

    def available_models(self) -> list[str]:
        """所有内置模型，统一 ``provider/model`` 形式。"""
        return [
            model_spec_for(provider.name, model)
            for provider in list_providers()
            for model in provider.models
        ]

    def default_model(self) -> str:
        """``chat()`` 不指定 model 时会用的模型（``provider/model`` 形式）。"""
        try:
            provider, model = self._resolve_model(None)
        except LLMError as exc:  # 配置写错了也别让展示界面的地方崩掉
            log.warning("默认模型配置有问题（%s），退回 DeepSeek 默认模型", exc)
            provider = get_provider("deepseek")
            model = provider.default_model
        return model_spec_for(provider.name, model)

    async def list_models(self, provider_name: str | None = None) -> list[str]:
        """返回该服务商账号下真实可用的模型 id 列表（不带 provider/ 前缀）。

        ``provider_name`` 为 ``None`` 时用默认模型所属的 provider。
        结果按 provider 缓存 10 分钟；失败（没 Key、HTTP 错误、网络/超时）
        抛 :class:`LLMError`，由上层决定是否回退到内置清单。
        这是查询接口，**不会重试**。
        """
        if provider_name is None or (
            isinstance(provider_name, str) and not provider_name.strip()
        ):
            provider = self._resolve_model(None)[0]
        else:
            try:
                provider = get_provider(provider_name)
            except ProviderError as exc:
                raise LLMError(str(exc)) from None

        cached = self._models_cache.get(provider.name)
        if cached is not None and time.monotonic() - cached[0] < _MODELS_CACHE_TTL:
            return list(cached[1])

        api_key = self._api_key(provider)  # 缺 Key 时抛带展示名和 --setup 的 LLMError
        base_url = self._base_url(provider)
        if provider.kind == "anthropic":
            url = _anthropic_models_url(base_url)
            headers = {
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
            }
        else:
            url = _join_url(base_url, _OPENAI_MODELS_SUFFIX)
            headers = {}
            if api_key:  # Ollama 之类本地服务不需要 Key
                headers["Authorization"] = f"Bearer {api_key}"

        models = await self._fetch_models(provider, url, headers)
        self._models_cache[provider.name] = (time.monotonic(), models)
        return list(models)

    def invalidate_models(self, provider_name: str | None = None) -> None:
        """清掉模型清单缓存；不传 ``provider_name`` 就全清。"""
        if provider_name is None:
            self._models_cache.clear()
            return
        key = provider_name.strip().lower() if isinstance(provider_name, str) else ""
        self._models_cache.pop(key, None)

    # ------------------------------------------------------------ 配置读取

    def _cfg_get(self, dotted: str, default: Any) -> Any:
        cfg = self.cfg
        if isinstance(cfg, dict):
            node: Any = cfg
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node
        getter = getattr(cfg, "get", None)
        if getter is None:
            return default
        try:
            return getter(dotted, default)
        except TypeError:  # 只接受一个参数的 getter
            value = getter(dotted)
            return default if value is None else value

    def _resolve_model(self, model: str | None) -> tuple[Provider, str]:
        spec = model
        if spec is None or (isinstance(spec, str) and not spec.strip()):
            spec = self._cfg_get("defaults.model", "") or None
            if isinstance(spec, str) and not spec.strip():
                spec = None
        try:
            return resolve_model(spec)
        except ProviderError as exc:
            raise LLMError(str(exc)) from None

    def _api_key(self, provider: Provider) -> str:
        value = self._cfg_get(f"providers.{provider.name}.api_key", "")
        key = str(value).strip() if value else ""
        if not key and provider.api_key_env:
            key = os.environ.get(provider.api_key_env, "").strip()
        if not key and provider.needs_key:
            hint = (
                f"请在配置文件里填写 providers.{provider.name}.api_key"
                f"，或设置环境变量 {provider.api_key_env}"
            )
            raise LLMError(
                f"没有配置 {provider.label} 的 API Key：{hint}；"
                f"也可以运行 --setup 重新配置。"
            )
        return key

    def _base_url(self, provider: Provider) -> str:
        value = self._cfg_get(f"providers.{provider.name}.base_url", "")
        url = str(value).strip() if value else ""
        return url or provider.base_url

    def _temperature(self) -> float:
        value = self._cfg_get("defaults.temperature", 1.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 1.0

    def _max_tokens(self) -> int:
        value = self._cfg_get("defaults.max_tokens", 2048)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 2048

    # ------------------------------------------------------------ 请求构造

    def _openai_request(
        self,
        provider: Provider,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        url = _join_url(base_url, _OPENAI_SUFFIX)
        headers = {"Content-Type": "application/json"}
        if api_key:  # Ollama 之类的本地服务不需要 Key
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        return url, payload, headers

    def _anthropic_request(
        self,
        provider: Provider,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        base = base_url.strip().rstrip("/")
        if base.endswith("/messages"):
            url = base
        elif base.endswith("/v1"):
            url = base + "/messages"
        else:
            url = base + _ANTHROPIC_SUFFIX

        # Anthropic 原生接口不允许 messages 里出现 system 角色，要抽到顶层
        system_parts: list[str] = []
        chat_messages: list[dict[str, str]] = []
        for message in messages:
            role = str(message.get("role", "")).strip().lower()
            content = message.get("content", "")
            if role == "system":
                text = str(content) if content is not None else ""
                if text:
                    system_parts.append(text)
                continue
            chat_messages.append({"role": role, "content": content})

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        system = "\n\n".join(system_parts).strip()
        if system:
            payload["system"] = system

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        return url, payload, headers

    # ------------------------------------------------------------ HTTP 层

    async def _get_session(self) -> aiohttp.ClientSession:
        session = self._session
        if session is not None and not session.closed:
            return session
        ctx = make_ssl_context()
        connector = aiohttp.TCPConnector(ssl=ctx) if ctx is not None else None
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )
        self._session = session
        return session

    async def _fetch_models(
        self, provider: Provider, url: str, headers: dict[str, str]
    ) -> list[str]:
        """GET 一次模型清单；不重试，失败统一转成中文 LLMError。"""
        try:
            session = await self._get_session()
            async with session.get(url, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    reason = self._http_error(provider, resp.status, text)
                    raise LLMError(
                        f"查询 {provider.label} 模型列表失败"
                        f"（HTTP {resp.status}）：{reason}"
                    )
                return self._extract_models(provider, self._loads(text))
        except LLMError:
            raise
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            raise LLMError(
                f"查询 {provider.label} 模型列表超时，检查网络或稍后再试"
            ) from None
        except aiohttp.ClientConnectorError:
            raise LLMError(f"连不上 {provider.label}，检查网络或代理设置") from None
        except aiohttp.ClientError as exc:
            raise LLMError(
                f"查询 {provider.label} 模型列表时网络异常（{exc.__class__.__name__}）"
            ) from None

    @staticmethod
    def _extract_models(provider: Provider, payload: Any) -> list[str]:
        """解析 OpenAI / Anthropic 风格的 ``{"data":[{"id": "..."}]}``。"""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise LLMError(f"{provider.label} 返回的模型列表格式不对（缺少 data 数组）")
        model_ids = {
            item["id"].strip()
            for item in data
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].strip()
        }
        return sorted(model_ids)

    async def _post(
        self,
        provider: Provider,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> str:
        last_error: LLMError | None = None

        for attempt in range(self.retries + 1):
            try:
                session = await self._get_session()
                async with session.post(url, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    if 200 <= resp.status < 300:
                        return self._extract(provider, self._loads(text))
                    if resp.status in _RETRY_STATUS and attempt < self.retries:
                        wait = self._backoff(attempt)
                        log.warning(
                            "%s 返回 %s，%.1fs 后重试（%d/%d）：%s",
                            provider.label,
                            resp.status,
                            wait,
                            attempt + 1,
                            self.retries,
                            text[:200],
                        )
                        await asyncio.sleep(wait)
                        continue
                    raise LLMError(self._http_error(provider, resp.status, text))

            except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
                last_error = LLMError(
                    f"请求 {provider.label} 响应超时（{self.timeout:.0f}s），稍后再试"
                )
                log.warning("%s 请求超时（第 %d 次）", provider.label, attempt + 1)
            except aiohttp.ClientConnectorError as exc:
                last_error = LLMError(f"连不上 {provider.label}，检查网络或代理设置")
                log.warning("%s 连接失败：%s", provider.label, exc)
            except aiohttp.ClientError as exc:
                last_error = LLMError(
                    f"请求 {provider.label} 时网络异常（{exc.__class__.__name__}）"
                )
                log.warning("%s 请求异常：%r", provider.label, exc)

            if attempt < self.retries:
                await asyncio.sleep(self._backoff(attempt))

        raise last_error or LLMError(f"请求 {provider.label} 失败")

    def _backoff(self, attempt: int) -> float:
        """指数退避：1.5s、3s、6s…"""
        try:
            base = float(self.retry_backoff)
        except (TypeError, ValueError):
            base = 1.5
        return base * (2 ** attempt)

    @staticmethod
    def _loads(text: str) -> Any:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            raise LLMError("接口返回的内容不是合法 JSON，可能 base_url 填错了") from None

    @staticmethod
    def _extract(provider: Provider, data: Any) -> str:
        if not isinstance(data, dict):
            raise LLMError(f"{provider.label} 返回内容格式不对")

        if provider.kind == "anthropic":
            blocks = data.get("content")
            if not isinstance(blocks, list):
                raise LLMError(f"{provider.label} 返回内容为空")
            text = "".join(
                str(block.get("text", ""))
                for block in blocks
                if isinstance(block, dict) and block.get("type", "text") == "text"
            )
        else:
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise LLMError(f"{provider.label} 返回内容为空") from None
            if isinstance(content, list):  # 少数兼容实现会返回分段内容
                text = "".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict)
                )
            elif content is None:
                text = ""
            else:
                text = str(content)

        text = text.strip()
        if not text:
            raise LLMError(f"{provider.label} 没有返回内容")
        return text

    @staticmethod
    def _http_error(provider: Provider, status: int, body: str) -> str:
        detail = ""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("type") or "")
            elif error:
                detail = str(error)
            if not detail:
                detail = str(data.get("message") or "")
        if not detail:
            detail = (body or "").strip()
        detail = " ".join(detail.split())[:160]

        tips = {
            400: "请求参数有误",
            401: f"{provider.label} 的 API Key 无效",
            402: f"{provider.label} 账户余额不足",
            403: f"{provider.label} 没有访问权限（检查 Key 权限或余额）",
            404: "模型或接口地址不对（检查模型名和 base_url）",
            405: "接口地址不对（检查 base_url）",
            422: "请求参数有误",
            429: f"{provider.label} 请求太频繁，被限流了",
        }
        if status in tips:
            head = tips[status]
        elif 500 <= status < 600:
            head = f"{provider.label} 服务端出错（{status}）"
        else:
            head = f"{provider.label} 返回 {status}"
        return f"{head}：{detail}" if detail else head


# ---------------------------------------------------------------- 小工具

def _join_url(base_url: str, suffix: str) -> str:
    """拼接接口地址，已经带后缀时原样返回。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return suffix
    if base.endswith(suffix):
        return base
    return base + suffix


def _anthropic_models_url(base_url: str) -> str:
    """Anthropic 的模型列表在 ``{base}/v1/models``，容忍 base 已带 /v1。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return _ANTHROPIC_MODELS_SUFFIX
    if base.endswith("/models"):
        return base
    if base.endswith("/v1"):
        return base + "/models"
    return base + _ANTHROPIC_MODELS_SUFFIX


def _preview(messages: list[dict[str, str]], limit: int = 60) -> str:
    for message in reversed(messages):
        if str(message.get("role", "")).lower() == "user":
            return _preview_text(str(message.get("content", "")), limit)
    return ""


def _preview_text(text: str, limit: int = 60) -> str:
    one_line = " ".join(str(text).split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"
