"""provider 注册表的健全性检查，防止「兜底名单被清空/写错」再次出现。

    python tests/test_providers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.providers import (  # noqa: E402
    PROVIDERS, ModelResolutionError, UnknownProvider, get_provider,
    list_providers, model_spec_for, resolve_model,
)

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


def main() -> int:
    print("[1] 注册表基本字段")
    for p in list_providers():
        check(f"{p.name}: kind 合法", p.kind in ("openai", "anthropic"), p.kind)
        check(f"{p.name}: base_url 是 http(s)", p.base_url.startswith("http"), p.base_url)
        check(f"{p.name}: 有环境变量名", bool(p.api_key_env), p.api_key_env)
        if p.default_model:
            check(f"{p.name}: 默认模型在名单里", p.default_model in p.models, p.default_model)
        else:
            check(f"{p.name}: 无默认模型时名单也应空", not p.models, str(p.models))

    print("\n[2] 重点 provider 的兜底名单不为空")
    for name in ("deepseek", "openai", "anthropic"):
        p = get_provider(name)
        check(f"{name} 有兜底模型", len(p.models) >= 2, str(p.models))
        check(f"{name} 有默认模型", bool(p.default_model), "")

    print("\n[3] 本地服务免 Key")
    check("ollama 不需要 Key", get_provider("ollama").needs_key is False)

    print("\n[4] 解析规则")
    provider, model = resolve_model("openai/gpt-6.2-astra")
    check("provider/model 正常解析", provider.name == "openai" and model == "gpt-6.2-astra")
    provider, model = resolve_model("openai/gpt-6.2-astra-pro:batch")
    check("模型名里的冒号保留", model == "gpt-6.2-astra-pro:batch", model)
    provider, model = resolve_model("anthropic/claude-opus-5.5")
    check("anthropic 解析", provider.name == "anthropic" and model == "claude-opus-5.5")
    provider, model = resolve_model(None)
    check("None 回落到 deepseek", provider.name == "deepseek" and bool(model), model)
    check("model_spec_for 往返一致",
          resolve_model(model_spec_for("openai", "gpt-6.2-astra"))[1] == "gpt-6.2-astra")

    try:
        resolve_model("nosuch/xxx")
        check("未知 provider 抛错", False, "没有抛异常")
    except UnknownProvider:
        check("未知 provider 抛错", True)
    try:
        resolve_model("openai/")
        check("空模型名抛错", False, "没有抛异常")
    except ModelResolutionError:
        check("空模型名抛错", True)

    print("\n[5] 名单无重复、无空白")
    for p in list_providers():
        check(f"{p.name}: 名单去重", len(set(p.models)) == len(p.models), str(p.models))
        check(f"{p.name}: 无空项", all(m.strip() for m in p.models), str(p.models))

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
