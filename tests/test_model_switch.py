"""多模型切换的集成测试：两条假模型服务 + 驱动 exe。

    python tests/test_model_switch.py

验证：默认模型生效 → /model 列出「实时查询到的」模型 → 切换后请求真的打到
另一家 → 切回来。假服务同时实现 /chat/completions 和 /models。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from e2e import (  # noqa: E402
    ExeProcess, action_texts, collect_actions, private_event, wait_port, write_config,
)

EXE = ROOT / "dist" / "qq-multi-model.exe"
ONEBOT_PORT = 18095
DS_PORT = 18096
OAI_PORT = 18097

DS_MODELS = ["deepseek-flash", "deepseek-v4-pro"]
OAI_MODELS = ["gpt-5", "gpt-5-mini"]

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


def mock_app(tag: str, models: list[str], seen: list[str]) -> web.Application:
    app = web.Application()

    async def chat(request: web.Request) -> web.Response:
        body = await request.json()
        seen.append(body.get("model", ""))
        content = f"{tag}:{body.get('model')}"
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": content}}]})

    async def list_models(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": m} for m in models]})

    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/v1/models", list_models)
    return app


CONFIG = """\
backend: onebot
defaults:
  model: "deepseek/deepseek-flash"
  timeout: 10
  retries: 0
providers:
  deepseek:
    api_key: "sk-ds"
    base_url: "http://127.0.0.1:{ds}/v1"
  openai:
    api_key: "sk-oai"
    base_url: "http://127.0.0.1:{oai}/v1"
chat:
  cooldown_seconds: 0
onebot:
  host: "127.0.0.1"
  port: {port}
  path: "/onebot"
  access_token: "tok"
""".format(ds=DS_PORT, oai=OAI_PORT, port=ONEBOT_PORT)


async def ask(ws, text: str) -> str:
    await ws.send_json(private_event(text, 20001))
    actions = await collect_actions(ws, 10)
    texts = [t for t in action_texts(actions) if t]
    return texts[0] if texts else ""


async def main() -> int:
    if not EXE.exists():
        print(f"找不到 exe：{EXE}（先跑 build.bat）")
        return 2

    seen_ds: list[str] = []
    seen_oai: list[str] = []
    runners = []
    for app, port in (
        (mock_app("deepseek", DS_MODELS, seen_ds), DS_PORT),
        (mock_app("openai", OAI_MODELS, seen_oai), OAI_PORT),
    ):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        runners.append(runner)

    work = TESTS / "tmp"
    work.mkdir(parents=True, exist_ok=True)
    cfg = write_config(work / "multi.yaml", CONFIG)

    try:
        async with ExeProcess(cfg, "multi", work):
            if not await wait_port(ONEBOT_PORT):
                check("exe 启动", False, "端口没开")
                return 1
            check("exe 启动", True)

            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    f"ws://127.0.0.1:{ONEBOT_PORT}/onebot",
                    headers={"Authorization": "Bearer tok", "X-Self-ID": "10000"},
                ) as ws:
                    reply = await ask(ws, "第一句")
                    check("默认走 deepseek", reply == "deepseek:deepseek-flash", reply)
                    check("用的是配置里的模型", seen_ds and seen_ds[-1] == "deepseek-flash",
                          str(seen_ds))

                    listing = await ask(ws, "/model")
                    check("/model 显示当前模型", "deepseek/deepseek-flash" in listing, listing[:160])
                    check("/model 列出实时模型（非内置）",
                          "deepseek/deepseek-v4-pro" in listing, listing[:160])
                    check("列表没混进别家", "openai/" not in listing, listing[:160])

                    other = await ask(ws, "/model openai")
                    check("/model openai 列出该家实时模型", "openai/gpt-5-mini" in other, other[:160])

                    switch = await ask(ws, "/model openai/gpt-5-mini")
                    check("切换有回执", "openai/gpt-5-mini" in switch, switch)

                    reply2 = await ask(ws, "第二句")
                    check("切换后打到 openai", reply2 == "openai:gpt-5-mini", reply2)
                    check("openai 收到正确模型名", seen_oai and seen_oai[-1] == "gpt-5-mini",
                          str(seen_oai))
                    check("deepseek 只被调用一次", len(seen_ds) == 1, str(seen_ds))

                    reset = await ask(ws, "/model reset")
                    check("reset 有回执", "默认" in reset, reset)

                    reply3 = await ask(ws, "第三句")
                    check("reset 后回到 deepseek", reply3.startswith("deepseek:"), reply3)

                    bad = await ask(ws, "/model nosuch/model")
                    check("未知服务商有友好报错", "认不出" in bad or "不认识" in bad, bad)
    finally:
        for runner in runners:
            await runner.cleanup()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
