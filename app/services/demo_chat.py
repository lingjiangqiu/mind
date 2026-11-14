"""Bounded public conversations. Never call the durable harness or MCP tools.

One process / one Uvicorn worker: state is RAM-only and expires with the signed
token. Each token has a random jti, so concurrent visitors never share history.
The runtime still executes the real agent nodes; only its memory adapter and
costly query/summary helpers are replaced with deterministic, read-only ones.
"""
from __future__ import annotations

import ipaddress
import json
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.agents.runtime import AgentContext, AgentRuntimeService, AgentStep
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.ai import has_consult_signal, has_high_risk_signal, split_text
from app.services.memory import compact_history_for_prompt
from app.services.philosophy import has_philosophy_risk_signal, unsafe_output_reason
from app.services.privacy import PrivacySanitizer


SAFETY_REPLY = (
    "听到你这样说，我很在意你此刻是否安全。先把这段对话放一放，"
    "尽量去有人陪伴的地方，把可能造成伤害的物品放远，马上联系一个能到你身边的可信任的人。\n\n"
    "如果你或他人可能马上受到伤害，请立即联系当地紧急服务；在中国大陆可拨打 110 或 120。"
    "也可以联系学校心理中心、辅导员或校园保卫部门，请他们帮你找到现实中的支持。\n\n"
    "这里是 AI 演示，没有真人监看，也不会代你联系任何人。你现在身边有谁可以马上过来陪你？"
)


@dataclass
class DemoConversation:
    session_id: str
    expires_at: int
    history: list[AiMessage] = field(default_factory=list)
    turns: int = 0
    busy: bool = False
    last_intent: IntentType = IntentType.CHAT
    safety_latched: bool = False


class DemoChatStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.conversations: dict[str, DemoConversation] = {}
        self.ip_requests: dict[str, deque[float]] = {}
        self.active = 0
        self.stop_event = threading.Event()
        self.worker = None

    def start(self, settings: Settings):
        self.stop_event.clear()
        def sweep():
            while not self.stop_event.wait(15):
                with self.lock:
                    self._prune(time.time(), settings)
        self.worker = threading.Thread(target=sweep, name="demo-memory-expiry", daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=2)
        with self.lock:
            self.conversations.clear()
            self.ip_requests.clear()

    def reserve(self, key: str, expiry: int, ip: str, message: str, session_id: str | None,
                settings: Settings) -> DemoConversation:
        if not message.strip() or len(message) > settings.demo_chat_max_chars:
            raise HTTPException(422, f"每条消息请填写 1–{settings.demo_chat_max_chars} 字。")
        now = time.time()
        with self.lock:
            self._prune(now, settings)
            if expiry <= now:
                raise HTTPException(401, "体验已到期，请重新进入。")
            state = self.conversations.get(key)
            if state is None:
                if session_id:
                    raise HTTPException(409, "临时对话已结束，请点击重新开始。")
                if len(self.conversations) >= settings.demo_chat_max_sessions:
                    raise HTTPException(503, "当前体验人数较多，请稍后再来。")
                state = DemoConversation(secrets.token_urlsafe(18), expiry)
                self.conversations[key] = state
            if session_id and session_id != state.session_id:
                raise HTTPException(403, "无法访问其他临时会话。")
            if state.busy:
                raise HTTPException(409, "上一条消息还在处理中，请稍等。")
            if state.turns >= settings.demo_chat_max_turns:
                raise HTTPException(429, "本次体验已达到轮数上限。你可以查看已有回应与处理过程。")
            if ip not in self.ip_requests and len(self.ip_requests) >= 4096:
                raise HTTPException(503, "当前体验人数较多，请稍后再来。")
            bucket = self.ip_requests.setdefault(ip, deque())
            if len(bucket) >= settings.demo_chat_ip_requests:
                raise HTTPException(429, "当前网络的体验次数已用完，请一小时后再试。", headers={"Retry-After": str(settings.demo_chat_window_seconds)})
            if self.active >= settings.demo_chat_max_concurrent:
                raise HTTPException(503, "AI 正在回应其他访客，请稍后重试。", headers={"Retry-After": "15"})
            bucket.append(now)
            state.turns += 1
            state.busy = True
            self.active += 1
            return state

    def _prune(self, now: float, settings: Settings):
        for key, state in list(self.conversations.items()):
            if state.expires_at <= now and not state.busy:
                del self.conversations[key]
        for ip, bucket in list(self.ip_requests.items()):
            while bucket and bucket[0] <= now - settings.demo_chat_window_seconds:
                bucket.popleft()
            if not bucket:
                del self.ip_requests[ip]

    def finish(self, state: DemoConversation):
        with self.lock:
            if state.busy:
                state.busy = False
                self.active = max(0, self.active - 1)
            if state.expires_at <= time.time():
                state.history.clear()

    def reset(self, key: str):
        with self.lock:
            state = self.conversations.get(key)
            if state:
                if state.busy:
                    raise HTTPException(409, "请等当前回复完成后再重新开始。")
                # Preserve spent quota and safety latch across a history reset.
                state.history.clear()
                state.session_id = secrets.token_urlsafe(18)
            return {"cleared": True, "turnsUsed": state.turns if state else 0}


DEMO_CHAT_STORE = DemoChatStore()


def demo_client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    # The backend has no published host port. Only trust nginx's overwritten
    # X-Real-IP when the immediate peer is on the private Docker network.
    try:
        if ipaddress.ip_address(peer).is_private:
            return str(ipaddress.ip_address(request.headers.get("x-real-ip", peer)))
    except ValueError:
        pass
    return peer


class PublicMemoryMixin:
    demo_state: DemoConversation

    def _create_memory_store(self, settings):
        return None

    def memory_agent(self, step: int, context: AgentContext) -> bool:
        if context.memory_loaded:
            return False
        history = list(self.demo_state.history)
        compacted, brief = compact_history_for_prompt(history, self.settings, context.model_input)
        context.model_history = self._bounded_model_history([*compacted, AiMessage(role="user", content=context.model_input)])
        # Include the recent actual dialogue, including assistant questions, so
        # short answers such as '是分数' resolve against the preceding question.
        recent = "\n".join(f"{m.role}: {m.content}" for m in history[-6:])
        context.memory_brief = (brief + "\n最近对话：\n" + recent)[-3500:] if history else "这是一次新的临时对话。"
        context.memory_loaded = True
        context.steps.append(AgentStep(step, "MemoryAgent", "READ_MEMORY", f"temporary messages={len(history)}"))
        return True

    def _classify(self, text: str, history: list[AiMessage]) -> IntentType:
        if self.demo_state.safety_latched or has_philosophy_risk_signal(text):
            return IntentType.RISK
        if has_consult_signal(text):
            return IntentType.CONSULT
        # Keep short follow-ups in the same context, unless they clearly ask
        # for an unrelated technical/general task.
        from app.agents.runtime import GENERAL_TASK_WORDS
        if self.demo_state.last_intent == IntentType.CONSULT and not any(w in text.lower() for w in GENERAL_TASK_WORDS):
            return IntentType.CONSULT
        return super()._classify(text, history)

    def _rewrite_query(self, context: AgentContext) -> str:
        recent_users = [m.content for m in self.demo_state.history if m.role == "user"]
        return " ".join([*recent_users[-2:], context.model_input])[-250:]

    def philosophy_agent(self, step: int, context: AgentContext) -> bool:
        # Preserve the initiating situation for theme/guard selection on later
        # terse replies. It remains data, never a system-level instruction.
        original = context.model_input
        recent_users = [m.content for m in self.demo_state.history if m.role == "user"]
        if recent_users:
            context.model_input = "此前困扰：" + "；".join(recent_users[-3:]) + "\n本轮回复：" + original
        try:
            return super().philosophy_agent(step, context)
        finally:
            context.model_input = original

    def counselor_agent(self, step: int, context: AgentContext) -> bool:
        if context.risk_assessed and context.risk_level == RiskLevel.HIGH and not context.response_planned:
            context.prepared_reply = SAFETY_REPLY
            context.response_planned = context.finished = True
            context.response_agent = "CounselorAgent"
            context.steps.append(AgentStep(step, "CounselorAgent", "SAFETY_RESPONSE", "philosophy bypassed; no tools or notifications"))
            return True
        return super().counselor_agent(step, context)


def public_runtime(db, settings, state):
    if settings.agent_framework.lower() == "langgraph":
        from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
        base = LangGraphAgentRuntimeService
    else:
        base = AgentRuntimeService
    runtime_type = type("PublicDemoRuntime", (PublicMemoryMixin, base), {})
    runtime = runtime_type(db, settings)
    runtime.demo_state = state
    return runtime


def _sse(kind: str, **payload) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **payload}, ensure_ascii=False)}\n\n"


STEP_LABELS = {
    "MemoryAgent": ("读取本次对话", "只使用当前临时会话的上下文"),
    "SupervisorAgent": ("选择回应路径", "区分普通问答、情绪支持和安全提示"),
    "KnowledgeAgent": ("查找校园支持资料", "检索内置资料，补充当前问题的支持信息"),
    "RiskGuardianAgent": ("检查安全信号", "先检查风险，再决定是否进入哲学整理"),
    "PhilosophyAgent": ("整理情绪与选择", "按适用情境选择视角，校验结构、措辞和行动"),
    "CounselorAgent": ("准备支持回应", "高风险时直接提供现实支持提示"),
    "CompanionAgent": ("准备日常回应", "普通问答不套用情绪整理结构"),
}


def run_public_turn(db, settings: Settings, user, state: DemoConversation, message: str):
    from types import SimpleNamespace
    sanitized = PrivacySanitizer().sanitize(message)
    result = public_runtime(db, settings, state).run(
        user, SimpleNamespace(id=None, public_id=state.session_id), sanitized, sanitized,
    )
    source = "safety" if result.risk_level == RiskLevel.HIGH else "model"
    fallback = None
    text = result.prepared_reply
    if result.philosophy:
        source = result.philosophy.source
        fallback = result.philosophy.fallback_reason
    if not text:
        try:
            from app.services.ai import AiClient
            text = AiClient(settings).complete(result.response_messages, max_tokens=650)
            if settings.ai_provider.lower() == "mock":
                source = "fallback"
                fallback = "model_disabled"
            if unsafe_output_reason(text) or re.search(r"【|】|斯多葛|存在主义|实用主义", text):
                raise ValueError("unsafe response")
        except Exception:
            text = "我在听。可以从你刚才提到的事情里，选一个最想聊的部分，告诉我现在具体卡在哪里？"
            source, fallback = "fallback", "model_error_or_validation"
    text = PrivacySanitizer().sanitize(text)
    if state.expires_at > time.time():
        state.history.extend([AiMessage(role="user", content=sanitized), AiMessage(role="assistant", content=text)])
        state.history[:] = state.history[-settings.demo_chat_max_turns * 2:]
        state.last_intent = result.intent
        state.safety_latched = state.safety_latched or result.risk_level == RiskLevel.HIGH
    steps = [{"agent": s.agent, "label": STEP_LABELS[s.agent][0], "detail": STEP_LABELS[s.agent][1]} for s in result.steps]
    steps.append({"agent": "OutputCheck", "label": "核对展示内容", "detail": {
        "model": "本轮模型生成的内容已通过适用检查",
        "fallback": "本轮采用预设安全回应，可继续追问",
        "safety": "跳过哲学解释，提供现实支持；没有发送通知",
    }[source]})
    return text, {"steps": steps, "source": source, "fallbackReason": fallback,
                  "lens": result.philosophy.lens if result.philosophy else None,
                  "safetyOverride": result.risk_level == RiskLevel.HIGH,
                  "memoryMessages": len(state.history), "persistentWrites": False,
                  "toolCalls": 0, "framework": settings.agent_framework}


async def stream_public_turn(db, settings, user, state, message):
    started = time.monotonic()
    try:
        yield _sse("meta", sessionId=state.session_id, expiresAt=state.expires_at,
                   turnsUsed=state.turns, maxTurns=settings.demo_chat_max_turns)
        yield _sse("status", message="正在读取上下文并准备回应…")
        text, trace = await run_in_threadpool(run_public_turn, db, settings, user, state, message)
        trace["elapsedMs"] = round((time.monotonic() - started) * 1000)
        yield _sse("trace", **trace)
        for chunk in split_text(text, 24):
            yield _sse("token", content=chunk)
        yield _sse("done", sessionId=state.session_id, source=trace["source"], turnsUsed=state.turns)
    except Exception:
        # Never return exception bodies: upstream errors can carry credentials.
        yield _sse("error", message="这次回应没有完成，请稍后重试。")
    finally:
        DEMO_CHAT_STORE.finish(state)
