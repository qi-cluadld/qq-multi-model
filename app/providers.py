"""多模型 provider 注册表。

把各家大模型的接入信息集中在这里：接口类型（OpenAI 兼容 / Anthropic 原生）、
默认 API 根地址、对应的环境变量名，以及内置推荐模型。

新增一个 OpenAI 兼容的服务，通常只需要在 ``PROVIDERS`` 里加一行：

    "myprovider": Provider(
        name="myprovider",
        label="MyProvider",
        kind="openai",
        base_url="https://api.example.com/v1",
        api_key_env="MYPROVIDER_API_KEY",
        models=("my-model",),
        default_model="my-model",
    ),

``models`` 只是界面上的推荐列表，``chat()`` 并不校验：用户完全可以在配置里
写一个列表里没有的模型名（例如新发布的模型）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """一个模型服务商的静态描述。"""

    name: str          # 小写标识，如 "deepseek"
    label: str         # 展示名，如 "DeepSeek"
    kind: str          # "openai"（OpenAI 兼容）| "anthropic"（Anthropic 原生）
    base_url: str      # 默认 API 根地址（chat 时再拼 /chat/completions 或 /v1/messages）
    api_key_env: str   # 对应环境变量名，如 "DEEPSEEK_API_KEY"
    models: tuple[str, ...]   # 内置推荐模型
    default_model: str
    needs_key: bool = True


# 顺序就是列表展示顺序，deepseek 放第一个（默认 provider）。
PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        name="deepseek",
        label="DeepSeek",
        kind="openai",
        # OpenAI 兼容：https://api.deepseek.com/chat/completions 也可用
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        # 2026-11 起实际在售的是这两个（旧版 deepseek-chat / deepseek-reasoner 已下线）
        models=("deepseek-flash", "deepseek-v4-pro"),
        default_model="deepseek-flash",
    ),
    "openai": Provider(
        name="openai",
        label="OpenAI",
        kind="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        # 兜底名单（2026-11 核对；账号的真实可用列表以 GET /models 为准）
        models=(
            "gpt-6.2-astra",
            "gpt-6.2-astra-pro",
            "gpt-6.2-sol",
            "gpt-6.1-terra",
            "gpt-6-luna",
        ),
        default_model="gpt-6.2-astra",
    ),
    "moonshot": Provider(
        name="moonshot",
        label="Kimi / Moonshot",
        kind="openai",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        models=("kimi-k3.1-pro", "kimi-k3.1-pro-highspeed", "kimi-k3"),
        default_model="kimi-k3.1-pro",
    ),
    "zhipu": Provider(
        name="zhipu",
        label="智谱 GLM",
        kind="openai",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="ZHIPU_API_KEY",
        models=("glm-5.3", "glm-5.2", "glm-5.3-flash"),
        default_model="glm-5.3",
    ),
    "qwen": Provider(
        name="qwen",
        label="通义千问",
        kind="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        models=("qwen4-max", "qwen3.7-plus", "qwen3.8-flash"),
        default_model="qwen3.7-plus",
    ),
    "ollama": Provider(
        name="ollama",
        label="Ollama（本地）",
        kind="openai",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env="OLLAMA_API_KEY",
        # 取决于你 `ollama pull` 过哪些；运行时用 GET /v1/models 列出本地已有的
        models=(),
        default_model="",
        needs_key=False,
    ),
    "openrouter": Provider(
        name="openrouter",
        label="OpenRouter",
        kind="openai",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        # OpenRouter 的模型 id 自带 "厂商/模型" 前缀，写成 provider/model 后
        # 仍然只按第一个 / 切分，所以 "openrouter/openai/gpt-4o-mini" 是合法的。
        # 模型池随上游变化，运行时拉取
        models=(),
        default_model="",
    ),
    "siliconflow": Provider(
        name="siliconflow",
        label="SiliconFlow 硅基流动",
        kind="openai",
        base_url="https://api.siliconflow.cn/v1",
        api_key_env="SILICONFLOW_API_KEY",
        models=(),
        default_model="",
    ),
    "anthropic": Provider(
        name="anthropic",
        label="Anthropic Claude",
        kind="anthropic",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        # 兜底名单（2026-11 核对；账号的真实可用列表以 GET /v1/models 为准）
        models=(
            "claude-opus-5.5",
            "claude-fable-5.2",
            "claude-sonnet-5.5",
            "claude-haiku-5.5",
        ),
        default_model="claude-opus-5.5",
    ),
}


# ---------------------------------------------------------------- 异常

class ProviderError(Exception):
    """provider 相关错误；message 是给用户看的中文。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UnknownProvider(ProviderError):
    """不认识的 provider 名字。"""


class ModelResolutionError(ProviderError):
    """模型名无法解析（找不到或有歧义）。"""


# ---------------------------------------------------------------- 对外 API

def get_provider(name: str) -> Provider:
    """按名字取 provider（大小写不敏感）。"""
    key = name.strip().lower() if isinstance(name, str) else ""
    provider = PROVIDERS.get(key)
    if provider is None:
        names = "、".join(PROVIDERS)
        raise UnknownProvider(f"不认识的 provider：{name!r}。可用的有：{names}")
    return provider


def list_providers() -> list[Provider]:
    """全部内置 provider（按注册顺序）。"""
    return list(PROVIDERS.values())


def resolve_model(spec: str | None) -> tuple[Provider, str]:
    """把模型写法解析成 ``(provider, 模型名)``。

    支持的写法：

    * ``None`` / 空串                → 默认 provider（deepseek）的默认模型
    * ``"deepseek/deepseek-chat"``    → 按第一个 ``/`` 切开
    * ``"deepseek/deepseek-ai/V3"``   → provider=deepseek，model=deepseek-ai/V3
    * ``"gpt-4o-mini"``               → 在所有内置 models 里找唯一匹配

    裸模型名找不到或不唯一时抛 :class:`ModelResolutionError`，
    provider 名字不认识时抛 :class:`UnknownProvider`。
    """
    text = spec.strip() if isinstance(spec, str) else ("" if spec is None else str(spec).strip())

    if not text:
        default = PROVIDERS["deepseek"]
        return default, default.default_model

    if "/" in text:
        name, _, model = text.partition("/")
        provider = get_provider(name)  # 不认识就抛 UnknownProvider
        model = model.strip()
        if not model:
            raise ModelResolutionError(
                f"模型名为空：{spec!r}。正确写法是 provider/model，"
                f"例如 {model_spec_for('deepseek', 'deepseek-chat')}"
            )
        return provider, model

    hits = [
        (provider, model)
        for provider in PROVIDERS.values()
        for model in provider.models
        if model == text
    ]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ModelResolutionError(
            f"不认识模型 {text!r}：内置推荐列表里没有它。"
            f"请用 provider/model 形式指定，例如 {model_spec_for('deepseek', 'deepseek-chat')}"
        )
    matched = "、".join(f"{provider.name}/{model}" for provider, model in hits)
    raise ModelResolutionError(
        f"模型 {text!r} 有多个匹配（{matched}），请用 provider/model 形式指定"
    )


def model_spec_for(provider_name: str, model: str) -> str:
    """反向拼接：``model_spec_for("deepseek", "deepseek-chat")`` → ``"deepseek/deepseek-chat"``。"""
    provider = get_provider(provider_name)
    text = model.strip() if isinstance(model, str) else ""
    if not text:
        raise ModelResolutionError(f"模型名为空（provider={provider.name!r}）")
    return f"{provider.name}/{text}"
