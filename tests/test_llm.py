"""多模型 LLM 客户端的离线测试。

    python tests/test_llm.py

全部使用本机 aiohttp 假服务，不联网；配置直接用一个有 ``.get(path, default)``
方法的 FakeConfig 构造，不需要真实 config.yaml。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

# Windows 管道/重定向时 Python 默认用 GBK，日志里的中文在 CI 里会变乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

from app import providers  # noqa: E402
from app.llm import LLMClient, LLMError  # noqa: E402
from app.providers import (  # noqa: E402
    ModelResolutionError,
    Provider,
    UnknownProvider,
    get_provider,
    list_providers,
    model_spec_for,
    resolve_model,
)


# ---------------------------------------------------------------- 测试辅助

class FakeConfig:
    """和 app.config.Config 一样的点号取值接口。"""

    def __init__(self, data: dict | None = None):
        self.data = data or {}

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class FakeAPI:
    """记录收到的请求，按队列返回预设响应。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: list[tuple[int, dict]] = []

    def reply(self, status: int, payload: dict) -> None:
        self.responses.append((status, payload))

    async def handle(self, request: web.Request) -> web.Response:
        raw = await request.read()
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "headers": {key.lower(): value for key, value in request.headers.items()},
                "body": body,
            }
        )
        if self.responses:
            status, payload = self.responses.pop(0)
        else:
            status, payload = 500, {"error": {"message": "测试服务器没有预设响应"}}
        return web.json_response(payload, status=status)


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    """每个测试起一个本地假 OpenAI/Anthropic 服务。"""

    async def asyncSetUp(self) -> None:
        self.api = FakeAPI()
        app = web.Application()
        app.router.add_route("POST", "/{tail:.*}", self.api.handle)
        app.router.add_route("GET", "/{tail:.*}", self.api.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.base_url = f"http://127.0.0.1:{port}"
        self._clients: list[LLMClient] = []

    async def asyncTearDown(self) -> None:
        for client in self._clients:
            await client.close()
        await self.runner.cleanup()

    def make_client(self, data: dict, **kwargs) -> LLMClient:
        client = LLMClient(FakeConfig(data), **kwargs)
        client.retry_backoff = 0.01  # 测试里别真等 1.5s
        self._clients.append(client)
        return client


# ---------------------------------------------------------------- provider 表

class TestProviders(unittest.TestCase):
    def test_default_model(self) -> None:
        expected = get_provider("deepseek").default_model
        self.assertTrue(expected, "deepseek 必须有内置默认模型")
        provider, model = resolve_model(None)
        self.assertEqual(provider.name, "deepseek")
        self.assertEqual(model, expected)
        self.assertEqual(resolve_model("")[1], expected)
        self.assertEqual(resolve_model("   ")[1], expected)

    def test_explicit_provider_model(self) -> None:
        provider, model = resolve_model("deepseek/deepseek-chat")
        self.assertEqual((provider.name, model), ("deepseek", "deepseek-chat"))

        provider, model = resolve_model("anthropic/claude-sonnet-4-20250514")
        self.assertEqual((provider.name, model), ("anthropic", "claude-sonnet-4-20250514"))

        # 只按第一个 / 切分
        provider, model = resolve_model("deepseek/deepseek-ai/DeepSeek-V3")
        self.assertEqual(provider.name, "deepseek")
        self.assertEqual(model, "deepseek-ai/DeepSeek-V3")

        provider, model = resolve_model("openrouter/openai/gpt-4o-mini")
        self.assertEqual(provider.name, "openrouter")
        self.assertEqual(model, "openai/gpt-4o-mini")

        # provider 名字大小写不敏感
        self.assertEqual(resolve_model("DeepSeek/deepseek-chat")[0].name, "deepseek")

    def test_bare_model_name(self) -> None:
        # 内置模型清单会随服务商更新，这里从当前注册表里挑一个唯一的裸模型名来解析
        counts: dict[str, int] = {}
        owners: dict[str, str] = {}
        for provider in list_providers():
            for model in provider.models:
                counts[model] = counts.get(model, 0) + 1
                owners[model] = provider.name
        unique = next((model for model, count in counts.items() if count == 1), None)
        self.assertIsNotNone(unique, "内置 provider 表里至少要有一个唯一的裸模型名")

        provider, model = resolve_model(unique)
        self.assertEqual((provider.name, model), (owners[unique], unique))

    def test_unknown_model(self) -> None:
        with self.assertRaises(ModelResolutionError) as ctx:
            resolve_model("this-model-does-not-exist")
        self.assertIn("provider/model", ctx.exception.message)

        # 有 provider 前缀但模型名为空
        with self.assertRaises(ModelResolutionError):
            resolve_model("deepseek/")

    def test_unknown_provider(self) -> None:
        with self.assertRaises(UnknownProvider):
            resolve_model("nope/some-model")
        with self.assertRaises(UnknownProvider):
            get_provider("nope")
        with self.assertRaises(UnknownProvider):
            get_provider("")

    def test_ambiguous_bare_model(self) -> None:
        shared = "shared-ambiguous-model"
        first = Provider(
            name="fake-one",
            label="Fake One",
            kind="openai",
            base_url="http://127.0.0.1:1/v1",
            api_key_env="FAKE_ONE_API_KEY",
            models=(shared,),
            default_model=shared,
        )
        second = Provider(
            name="fake-two",
            label="Fake Two",
            kind="openai",
            base_url="http://127.0.0.1:1/v1",
            api_key_env="FAKE_TWO_API_KEY",
            models=(shared,),
            default_model=shared,
        )
        with mock.patch.dict(
            providers.PROVIDERS, {"fake-one": first, "fake-two": second}
        ):
            with self.assertRaises(ModelResolutionError) as ctx:
                resolve_model(shared)
        self.assertIn("多个", ctx.exception.message)

    def test_model_spec_for(self) -> None:
        self.assertEqual(
            model_spec_for("deepseek", "deepseek-chat"), "deepseek/deepseek-chat"
        )
        self.assertEqual(model_spec_for("OpenAI", "gpt-4o-mini"), "openai/gpt-4o-mini")
        with self.assertRaises(UnknownProvider):
            model_spec_for("nope", "x")
        with self.assertRaises(ModelResolutionError):
            model_spec_for("deepseek", "")

    def test_builtin_provider_table(self) -> None:
        names = [provider.name for provider in list_providers()]
        self.assertEqual(names[0], "deepseek")  # deepseek 是默认 provider
        for required in (
            "deepseek",
            "openai",
            "moonshot",
            "zhipu",
            "qwen",
            "ollama",
            "openrouter",
            "siliconflow",
            "anthropic",
        ):
            self.assertIn(required, names)
        self.assertFalse(get_provider("ollama").needs_key)
        self.assertEqual(get_provider("ollama").base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(get_provider("anthropic").kind, "anthropic")
        self.assertEqual(get_provider("deepseek").kind, "openai")
        self.assertEqual(get_provider("deepseek").api_key_env, "DEEPSEEK_API_KEY")
        for provider in list_providers():
            self.assertTrue(provider.base_url.startswith("http"), provider.name)
            # 部分 provider 的模型清单改成运行时拉取（models 为空），允许
            if provider.models:
                self.assertTrue(provider.default_model, provider.name)
                self.assertIn(provider.default_model, provider.models, provider.name)

    def test_available_models(self) -> None:
        client = LLMClient(FakeConfig())
        expected = [
            f"{provider.name}/{model}"
            for provider in list_providers()
            for model in provider.models
        ]
        models = client.available_models()
        self.assertEqual(models, expected)
        self.assertEqual(len(models), len(set(models)))
        # deepseek 一定有内置推荐模型，默认模型展示与 resolve_model(None) 一致
        self.assertTrue(any(model.startswith("deepseek/") for model in models))
        provider, model = resolve_model(None)
        self.assertEqual(client.default_model(), model_spec_for(provider.name, model))


# ---------------------------------------------------------------- OpenAI 兼容

class TestOpenAIKind(ServerTestCase):
    async def test_request_headers_body_and_reply(self) -> None:
        self.api.reply(200, {"choices": [{"message": {"content": "你好呀"}}]})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk-test", "base_url": self.base_url}
                },
                "defaults": {"temperature": 0.5, "max_tokens": 128},
            }
        )

        reply = await client.chat(
            [{"role": "user", "content": "hi"}], model="deepseek/deepseek-chat"
        )
        self.assertEqual(reply, "你好呀")
        self.assertEqual(len(self.api.requests), 1)

        request = self.api.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/chat/completions")
        self.assertEqual(request["headers"].get("authorization"), "Bearer sk-test")
        body = request["body"]
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(body["temperature"], 0.5)
        self.assertEqual(body["max_tokens"], 128)
        self.assertIs(body["stream"], False)

    async def test_default_model_from_cfg(self) -> None:
        self.api.reply(200, {"choices": [{"message": {"content": "ok"}}]})
        client = self.make_client(
            {
                "providers": {
                    "openai": {"api_key": "sk-openai", "base_url": self.base_url}
                },
                "defaults": {"model": "openai/gpt-4o-mini"},
            }
        )

        reply = await client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(reply, "ok")
        request = self.api.requests[0]
        self.assertEqual(request["body"]["model"], "gpt-4o-mini")
        self.assertEqual(request["headers"].get("authorization"), "Bearer sk-openai")
        self.assertEqual(client.default_model(), "openai/gpt-4o-mini")

    async def test_api_key_from_env(self) -> None:
        self.api.reply(200, {"choices": [{"message": {"content": "env"}}]})
        client = self.make_client(
            {"providers": {"deepseek": {"base_url": self.base_url}}}
        )
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-from-env"}):
            reply = await client.chat(
                [{"role": "user", "content": "hi"}], model="deepseek/deepseek-chat"
            )
        self.assertEqual(reply, "env")
        self.assertEqual(
            self.api.requests[0]["headers"].get("authorization"), "Bearer sk-from-env"
        )

    async def test_config_key_beats_env(self) -> None:
        self.api.reply(200, {"choices": [{"message": {"content": "cfg"}}]})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk-config", "base_url": self.base_url}
                }
            }
        )
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env"}):
            await client.chat(
                [{"role": "user", "content": "hi"}], model="deepseek/deepseek-chat"
            )
        self.assertEqual(
            self.api.requests[0]["headers"].get("authorization"), "Bearer sk-config"
        )


# ---------------------------------------------------------------- Anthropic

class TestAnthropicKind(ServerTestCase):
    async def test_system_moved_to_top_level(self) -> None:
        self.api.reply(
            200, {"content": [{"type": "text", "text": "我是 Claude 的回复"}]}
        )
        client = self.make_client(
            {
                "providers": {
                    "anthropic": {"api_key": "sk-ant", "base_url": self.base_url}
                },
                "defaults": {"temperature": 0.3, "max_tokens": 256},
            }
        )
        messages = [
            {"role": "system", "content": "你是一个简洁的助手"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好"},
            {"role": "user", "content": "再说一次"},
        ]

        reply = await client.chat(messages, model="anthropic/claude-sonnet-4-20250514")
        self.assertEqual(reply, "我是 Claude 的回复")

        request = self.api.requests[0]
        self.assertEqual(request["path"], "/v1/messages")
        self.assertEqual(request["headers"].get("x-api-key"), "sk-ant")
        self.assertEqual(request["headers"].get("anthropic-version"), "2023-06-01")
        body = request["body"]
        self.assertEqual(body["system"], "你是一个简洁的助手")
        self.assertEqual(body["model"], "claude-sonnet-4-20250514")
        self.assertEqual(body["max_tokens"], 256)
        self.assertEqual(body["temperature"], 0.3)
        roles = [message["role"] for message in body["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])
        self.assertTrue(all(role != "system" for role in roles))


# ---------------------------------------------------------------- 模型列表查询

class TestListModels(ServerTestCase):
    async def test_returns_sorted_unique_ids(self) -> None:
        self.api.reply(
            200,
            {
                "data": [
                    {"id": "model-c"},
                    {"id": "model-a"},
                    {"id": "model-b"},
                    {"id": "model-a"},  # 重复项去掉
                    {"object": "model"},  # 没有 id 的条目忽略
                ]
            },
        )
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk-list", "base_url": self.base_url}
                }
            }
        )

        models = await client.list_models("deepseek")
        self.assertEqual(models, ["model-a", "model-b", "model-c"])

        request = self.api.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"], "/models")
        self.assertEqual(request["headers"].get("authorization"), "Bearer sk-list")

    async def test_default_provider_from_cfg(self) -> None:
        self.api.reply(200, {"data": [{"id": "gpt-4o-mini"}]})
        client = self.make_client(
            {
                "providers": {
                    "openai": {"api_key": "sk-openai", "base_url": self.base_url}
                },
                "defaults": {"model": "openai/gpt-4o-mini"},
            }
        )

        models = await client.list_models()  # 不传 provider，用默认模型的 provider
        self.assertEqual(models, ["gpt-4o-mini"])
        self.assertEqual(self.api.requests[0]["path"], "/models")
        self.assertEqual(
            self.api.requests[0]["headers"].get("authorization"), "Bearer sk-openai"
        )

    async def test_cache_hits_within_ttl(self) -> None:
        self.api.reply(200, {"data": [{"id": "m1"}]})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk", "base_url": self.base_url}
                }
            }
        )

        first = await client.list_models("deepseek")
        second = await client.list_models("deepseek")
        self.assertEqual(first, ["m1"])
        self.assertEqual(second, ["m1"])
        self.assertEqual(len(self.api.requests), 1)  # 第二次命中缓存

    async def test_invalidate_models_refetches(self) -> None:
        self.api.reply(200, {"data": [{"id": "old"}]})
        self.api.reply(200, {"data": [{"id": "new"}]})
        self.api.reply(200, {"data": [{"id": "again"}]})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk", "base_url": self.base_url}
                }
            }
        )

        self.assertEqual(await client.list_models("deepseek"), ["old"])
        self.assertEqual(len(self.api.requests), 1)

        client.invalidate_models("deepseek")
        self.assertEqual(await client.list_models("deepseek"), ["new"])
        self.assertEqual(len(self.api.requests), 2)

        client.invalidate_models()  # 不传参数清全部
        self.assertEqual(await client.list_models("deepseek"), ["again"])
        self.assertEqual(len(self.api.requests), 3)

    async def test_cache_is_per_provider(self) -> None:
        self.api.reply(200, {"data": [{"id": "deepseek-chat"}]})
        self.api.reply(200, {"data": [{"id": "gpt-4o-mini"}]})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk-d", "base_url": self.base_url},
                    "openai": {"api_key": "sk-o", "base_url": self.base_url},
                }
            }
        )

        self.assertEqual(await client.list_models("deepseek"), ["deepseek-chat"])
        self.assertEqual(await client.list_models("openai"), ["gpt-4o-mini"])
        self.assertEqual(len(self.api.requests), 2)

        # 两个 provider 各自命中缓存
        await client.list_models("deepseek")
        await client.list_models("openai")
        self.assertEqual(len(self.api.requests), 2)

    async def test_401_raises_llm_error_without_retry(self) -> None:
        self.api.reply(401, {"error": {"message": "Invalid API key"}})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk-bad", "base_url": self.base_url}
                }
            },
            retries=2,
        )

        with self.assertRaises(LLMError) as ctx:
            await client.list_models("deepseek")
        message = ctx.exception.message
        self.assertIn("401", message)       # 带状态码
        self.assertIn("DeepSeek", message)  # 带展示名
        self.assertIn("API Key", message)
        self.assertEqual(len(self.api.requests), 1)  # 查询失败不重试

    async def test_missing_api_key(self) -> None:
        client = self.make_client({})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            with self.assertRaises(LLMError) as ctx:
                await client.list_models("deepseek")
        message = ctx.exception.message
        self.assertIn("DeepSeek", message)
        self.assertIn("--setup", message)
        self.assertEqual(self.api.requests, [])

    async def test_anthropic_uses_v1_models(self) -> None:
        self.api.reply(
            200,
            {
                "data": [
                    {"id": "claude-sonnet-4-20250514", "type": "model"},
                    {"id": "claude-3-5-haiku-20241022", "type": "model"},
                ]
            },
        )
        client = self.make_client(
            {
                "providers": {
                    "anthropic": {"api_key": "sk-ant", "base_url": self.base_url}
                }
            }
        )

        models = await client.list_models("anthropic")
        self.assertEqual(
            models,
            ["claude-3-5-haiku-20241022", "claude-sonnet-4-20250514"],
        )
        request = self.api.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"], "/v1/models")
        self.assertEqual(request["headers"].get("x-api-key"), "sk-ant")
        self.assertEqual(request["headers"].get("anthropic-version"), "2023-06-01")

    async def test_unknown_provider(self) -> None:
        client = self.make_client({})
        with self.assertRaises(LLMError):
            await client.list_models("nope")

    async def test_malformed_response(self) -> None:
        self.api.reply(200, {"unexpected": []})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk", "base_url": self.base_url}
                }
            }
        )
        with self.assertRaises(LLMError) as ctx:
            await client.list_models("deepseek")
        self.assertIn("格式", ctx.exception.message)


# ---------------------------------------------------------------- 错误处理

class TestErrors(ServerTestCase):
    async def test_missing_api_key(self) -> None:
        client = self.make_client({})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            with self.assertRaises(LLMError) as ctx:
                await client.chat(
                    [{"role": "user", "content": "hi"}],
                    model="deepseek/deepseek-chat",
                )
        message = ctx.exception.message
        self.assertIn("DeepSeek", message)      # 带展示名
        self.assertIn("API Key", message)
        self.assertIn("--setup", message)       # 提示怎么配置
        self.assertIn("DEEPSEEK_API_KEY", message)
        self.assertEqual(self.api.requests, [])  # 没发请求

    async def test_ollama_does_not_need_key(self) -> None:
        self.api.reply(200, {"choices": [{"message": {"content": "本地"}}]})
        client = self.make_client(
            {"providers": {"ollama": {"base_url": self.base_url}}}
        )
        reply = await client.chat(
            [{"role": "user", "content": "hi"}], model="ollama/qwen2.5:7b"
        )
        self.assertEqual(reply, "本地")
        self.assertNotIn("authorization", self.api.requests[0]["headers"])

    async def test_401_chinese_error(self) -> None:
        self.api.reply(401, {"error": {"message": "Invalid API key"}})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk-bad", "base_url": self.base_url}
                }
            }
        )
        with self.assertRaises(LLMError) as ctx:
            await client.chat(
                [{"role": "user", "content": "hi"}], model="deepseek/deepseek-chat"
            )
        message = ctx.exception.message
        self.assertIn("API Key", message)
        self.assertIn("无效", message)
        self.assertIn("DeepSeek", message)
        self.assertEqual(len(self.api.requests), 1)  # 401 不重试

    async def test_429_retry_then_success(self) -> None:
        self.api.reply(429, {"error": {"message": "rate limit"}})
        self.api.reply(200, {"choices": [{"message": {"content": "重试成功"}}]})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk", "base_url": self.base_url}
                }
            },
            retries=2,
        )

        reply = await client.chat(
            [{"role": "user", "content": "hi"}], model="deepseek/deepseek-chat"
        )
        self.assertEqual(reply, "重试成功")
        self.assertEqual(len(self.api.requests), 2)
        self.assertGreaterEqual(len(self.api.requests), 2)

    async def test_retry_exhausted(self) -> None:
        for _ in range(3):
            self.api.reply(429, {"error": {"message": "rate limit"}})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk", "base_url": self.base_url}
                }
            },
            retries=2,
        )

        with self.assertRaises(LLMError) as ctx:
            await client.chat(
                [{"role": "user", "content": "hi"}], model="deepseek/deepseek-chat"
            )
        self.assertIn("限流", ctx.exception.message)
        self.assertEqual(len(self.api.requests), 3)  # 首次 + 2 次重试

    async def test_5xx_is_translated(self) -> None:
        self.api.reply(503, {"error": {"message": "overloaded"}})
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {"api_key": "sk", "base_url": self.base_url}
                }
            },
            retries=0,
        )
        with self.assertRaises(LLMError) as ctx:
            await client.chat(
                [{"role": "user", "content": "hi"}], model="deepseek/deepseek-chat"
            )
        self.assertIn("服务端出错", ctx.exception.message)

    async def test_connection_error(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        client = self.make_client(
            {
                "providers": {
                    "deepseek": {
                        "api_key": "sk",
                        "base_url": f"http://127.0.0.1:{closed_port}",
                    }
                }
            },
            retries=0,
        )
        with self.assertRaises(LLMError) as ctx:
            await client.chat(
                [{"role": "user", "content": "hi"}], model="deepseek/deepseek-chat"
            )
        self.assertIn("连不上", ctx.exception.message)

    async def test_unknown_model_raises_llm_error(self) -> None:
        client = self.make_client({})
        with self.assertRaises(LLMError) as ctx:
            await client.chat([{"role": "user", "content": "hi"}], model="deepseek/")
        self.assertIn("模型名", ctx.exception.message)


# ---------------------------------------------------------------- 入口

def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
    print("\n" + "=" * 52)
    print(f"通过 {total - failed} 项，失败 {failed} 项")
    for test, trace in result.failures + result.errors:
        print(f"  - {test}")
        print(trace)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
