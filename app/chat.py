"""会话管理 + 对话编排（多轮上下文、限流、并发保护、命令）。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .llm import LLMClient, LLMError
from .providers import PROVIDERS, ProviderError, get_provider, model_spec_for, resolve_model

log = logging.getLogger("bot.chat")

HELP_TEXT = (
    "我是接了大模型的 QQ 助手，直接发消息就能聊。\n"
    "可用命令：\n"
    "/model —— 看当前模型和可用模型\n"
    "/model 名字 —— 切换模型（如 /model openai/gpt-4o-mini）\n"
    "/reset 或 /clear —— 清空当前对话上下文，重新开始\n"
    "/help —— 看这条帮助"
)


@dataclass
class Session:
    messages: list[dict[str, str]] = field(default_factory=list)
    last_active: float = field(default_factory=time.time)
    model: str | None = None  # 这个会话单独指定的模型，None 表示用默认

    def touch(self) -> None:
        self.last_active = time.time()


class ChatService:
    def __init__(self, cfg, client: LLMClient):
        self.cfg = cfg
        self.client = client
        self.system_prompt = cfg.get("defaults.system_prompt", "")
        self.history_turns = int(cfg.get("chat.history_turns", 10))
        self.ttl = float(cfg.get("chat.session_ttl_minutes", 30)) * 60
        self.max_sessions = int(cfg.get("chat.max_sessions", 500))
        self.cooldown = float(cfg.get("chat.cooldown_seconds", 1.5))
        self.max_concurrent = int(cfg.get("chat.max_concurrent", 4))
        self.busy_reply = cfg.get("chat.busy_reply", "稍等，我这边有点忙~")
        self.cooldown_reply = cfg.get("chat.cooldown_reply", "慢一点~")
        self.error_reply = cfg.get("chat.error_reply", "出错了（{reason}）")

        self._sessions: dict[str, Session] = {}
        self._last_request: dict[str, float] = {}
        self._sem = asyncio.Semaphore(self.max_concurrent)

    # ------------------------------------------------------------ 对外入口

    async def handle(self, key: str, text: str) -> str | None:
        """返回要发回 QQ 的文本；返回 None 表示不回复。"""
        text = (text or "").strip()
        if not text:
            return None

        command = text.split()[0].lower() if text else ""
        if command in ("/help", "/?", "help"):
            return HELP_TEXT
        if command in ("/reset", "/clear", "/new"):
            self._sessions.pop(key, None)
            return "上下文已清空，我们重新开始聊吧。"
        if command in ("/model", "/models"):
            return await self._handle_model(key, text)

        now = time.time()
        last = self._last_request.get(key, 0.0)
        if now - last < self.cooldown:
            return self.cooldown_reply
        self._last_request[key] = now

        # 限制并发：拿不到信号量就直接回“忙”，不排队堆积
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=0.05)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return self.busy_reply

        try:
            session = self._get_session(key)
            session.messages.append({"role": "user", "content": text})
            payload = self._build_payload(session)

            log.info("→ LLM [%s] 模型=%s %s", key, session.model or self.client.default_model(),
                     _preview(text))
            started = time.perf_counter()
            try:
                reply = await self.client.chat(payload, model=session.model)
            except LLMError as exc:
                session.messages.pop()  # 失败的这轮别留在上下文里
                log.warning("模型调用失败 [%s]：%s", key, exc.message)
                return self.error_reply.format(reason=exc.message)

            elapsed = time.perf_counter() - started
            session.messages.append({"role": "assistant", "content": reply})
            self._trim(session)
            session.touch()
            self._evict_if_needed()
            log.info("← LLM [%s] %.1fs %s", key, elapsed, _preview(reply))
            return reply
        finally:
            self._sem.release()

    # ------------------------------------------------------------ 内部

    async def _handle_model(self, key: str, text: str) -> str:
        session = self._get_session(key)
        parts = text.split()
        current = session.model or self.client.default_model()

        if len(parts) == 1:
            provider_name = None
            try:
                provider_name = resolve_model(current)[0].name
            except ProviderError:
                pass
            return await self._describe_models(current, provider_name)

        arg = parts[1]
        if arg.lower() in ("reset", "default", "默认"):
            session.model = None
            return f"已切回默认模型：{self.client.default_model()}"

        # "/model openai" —— 只看这家有哪些模型
        if "/" not in arg and arg.lower() in PROVIDERS:
            return await self._describe_models(current, arg.lower())

        try:
            provider, model = resolve_model(arg)
        except ProviderError as exc:
            return f"认不出这个模型：{exc}"
        session.model = model_spec_for(provider.name, model)
        log.info("会话 [%s] 切换到 %s", key, session.model)
        return f"已切换到 {session.model}（上下文保留；用 /reset 可清空）"

    async def _describe_models(self, current: str, provider_name: str | None) -> str:
        """列出某家账号下真实可用的模型（实时查询，失败回退内置清单）。"""
        if not provider_name:
            return f"当前模型：{current}\n发 /model <provider> 可以看某家的模型"

        provider = get_provider(provider_name)
        lines = [f"当前模型：{current}", ""]
        try:
            models = await self.client.list_models(provider_name)
            lines.append(f"{provider.label} 账号下的模型（实时）：")
        except LLMError as exc:
            models = list(provider.models)
            lines.append(f"{provider.label} 实时查询失败（{exc.message}），下面是内置兜底：")

        if not models:
            lines.append("  （没查到可用模型；可直接用 provider/模型名 指定）")
        for name in models[:20]:
            lines.append(f"  {model_spec_for(provider.name, name)}")
        if len(models) > 20:
            lines.append(f"  …还有 {len(models) - 20} 个")
        lines.append("")
        lines.append("/model <名字> 切换，/model reset 回默认")
        return "\n".join(lines)

    def _get_session(self, key: str) -> Session:
        session = self._sessions.get(key)
        if session is None or time.time() - session.last_active > self.ttl:
            if session is not None:
                log.info("会话 [%s] 闲置超时，上下文已重置", key)
            session = Session()
            self._sessions[key] = session
        return session

    def _build_payload(self, session: Session) -> list[dict[str, str]]:
        payload: list[dict[str, str]] = []
        if self.system_prompt:
            payload.append({"role": "system", "content": self.system_prompt})
        payload.extend(session.messages)
        return payload

    def _trim(self, session: Session) -> None:
        keep = max(1, self.history_turns) * 2
        if len(session.messages) > keep:
            session.messages = session.messages[-keep:]

    def _evict_if_needed(self) -> None:
        overflow = len(self._sessions) - self.max_sessions
        if overflow <= 0:
            return
        # 按最后活跃时间淘汰最老的
        ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].last_active)
        for key, _ in ordered[:overflow]:
            self._sessions.pop(key, None)
            self._last_request.pop(key, None)
        log.info("会话数超过上限，清理了 %d 个最旧会话", overflow)


def _preview(text: str, limit: int = 60) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


def split_text(text: str, limit: int) -> list[str]:
    """把长回复切成若干条不超过 limit 的短消息，尽量在换行/句号处断开。"""
    if limit <= 0 or len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("；"),
            window.rfind(" "),
        )
        if cut < limit // 2:  # 断点太靠前就硬切
            cut = limit
        else:
            cut += 1
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest.strip():
        chunks.append(rest.strip())
    return [c for c in chunks if c]
