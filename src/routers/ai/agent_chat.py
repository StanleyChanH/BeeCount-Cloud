"""POST /api/v1/ai/agent/chat — 悬浮 AI 财务助手(Agent 模式)。

跟 /ai/ask(文档 RAG,只答文档)不同,这里让用户自配的 LLM 通过**工具调用**
读到自己的账本数据,做查询 / 分析 / 建议:
  get_analytics / get_insights / get_budgets / get_net_worth /
  list_transactions / list_ledgers —— 全部只读,复用 MCP read_tools 的
  生产口径(软删账本过滤、exclude_from_stats、本位币折算)。

工具调用协议:JSON 模式 ReAct(不用原生 function calling —— 仓库的
provider 层只保证 OpenAI-compatible /chat/completions 的稳定子集,任意
网关对 tools 参数支持参差;call_chat_json 已带鲁棒 JSON 抽取 + 自适应
摘参,在其上用 JSON envelope 模拟工具调用对各家兼容性最好):
  模型每轮必须回 {"action":"answer","answer":...} 或
  {"action":"tool","tool":...,"args":{...}};工具结果作为 user 消息
  (TOOL_RESULT)回灌,最多 _MAX_TOOL_ROUNDS 轮,超限强制"直接作答"。

SSE 事件(前端悬浮窗):
  status(chunk 之前的工具执行进度,前端本地化工具名)
  chunk(最终答案一次性发 — 循环模式下无法逐 token 流式,前端做打字机)
  tool_calls(本轮工具轨迹) / done / error

错误码:AI_NO_CHAT_PROVIDER(400)/ AI_AGENT_RATE_LIMITED(429)/
AI_PROVIDER_ERROR / AI_AGENT_INVALID_OUTPUT(stream 内)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from datetime import timezone as _tz

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...database import get_db
from ...deps import get_current_user, require_any_scopes
from ...mcp.tools import read_tools
from ...models import User, UserProfile
from ...security import SCOPE_APP_WRITE, SCOPE_WEB_READ, SCOPE_WEB_WRITE
from ...services.ai import (
    ChatProviderError,
    NoChatProviderError,
    resolve_chat_provider,
)
from ...services.ai.provider_client import call_chat_json

logger = logging.getLogger(__name__)
router = APIRouter()

_AGENT_SCOPE_DEP = require_any_scopes(SCOPE_APP_WRITE, SCOPE_WEB_READ, SCOPE_WEB_WRITE)

# ──────────────── 速率限制(内存,test_provider 同款 deque) ────────────────

_RATE_WINDOWS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LIMIT_WINDOW_S = 60.0
# Agent 一次请求 = 多次 LLM 调用(1~8 次),比 test_provider 的 30 更紧
_RATE_LIMIT_MAX = 20


def _check_rate_limit(user_id: str) -> bool:
    now = time.monotonic()
    window = _RATE_WINDOWS[user_id]
    while window and now - window[0] > _RATE_LIMIT_WINDOW_S:
        window.popleft()
    if len(window) >= _RATE_LIMIT_MAX:
        return False
    window.append(now)
    return True


# ──────────────── 请求模型 ────────────────

_MAX_HISTORY = 10  # 客户端最多带 10 条历史(20 条 messages)
_MAX_CONTENT_CHARS = 2000
_MAX_TOOL_ROUNDS = 6
_MAX_TOOL_RESULT_CHARS = 6000  # 工具结果截断,防 prompt 爆炸


class AgentChatMessageIn(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str = Field(min_length=1, max_length=_MAX_CONTENT_CHARS)


class AgentChatRequest(BaseModel):
    messages: list[AgentChatMessageIn] = Field(min_length=1, max_length=_MAX_HISTORY * 2)
    locale: str = Field(default="zh", pattern=r"^(zh|zh-CN|zh-TW|en)$")
    tz_offset_minutes: int = Field(default=0, ge=-720, le=840)


# ──────────────── 工具层(全部只读) ────────────────


def _tool_get_net_worth(user: User, *, ledger_id: str | None = None, tz_offset_minutes: int = 0) -> dict:
    """月度净资产序列(主币种口径)。read_tools 没有现成包装,这里照 MCP 的
    router-function 直调模式包一层。注意 SessionLocal 经 read_tools 模块属性
    取 —— 测试会 monkeypatch 它(read_tools 不走 FastAPI dep tree)。"""
    from ...routers.read.workspace import workspace_net_worth_history

    with read_tools.SessionLocal() as db:
        led = read_tools._resolve_ledger(db, user.id, ledger_id)
        if not led:
            return {"error": "ledger not found"}
        result = workspace_net_worth_history(
            ledger_id=led.external_id, user_id=led.user_id,
            tz_offset_minutes=tz_offset_minutes,
            _scopes=set(), current_user=user, db=db,
        )
        data = result.model_dump(mode="json")
    series = data.get("series") or []
    return {
        "multi_currency": data.get("multi_currency", False),
        # 只给尾部 12 个月 + 全期首点,足够模型看趋势又省 token
        "series": ([series[0]] if series else []) + series[-12:],
    }


# LLM 可调用的工具注册表:name → (描述+参数说明, 同步实现)。
# 实现签名统一 (user, **kwargs) → JSON-serializable dict;内部异常一律转
# {"error": ...} 观测值回灌给模型自己纠错,不打断 SSE。
_TOOL_SPECS: dict[str, dict] = {
    "list_ledgers": {
        "desc": "List the user's ledgers (id, name, currency, month_start_day, shared).",
        "args": {},
        "fn": lambda user, **kw: read_tools.list_ledgers(user),
    },
    "get_analytics": {
        "desc": (
            "Income/expense/balance summary + daily/monthly series + top categories for a period. "
            "scope: 'month'|'year'|'all'; period: 'YYYY-MM' (month) or 'YYYY' (year), default current."
        ),
        "args": {"scope": "month", "period": None, "ledger_id": None},
        "fn": read_tools.get_analytics_summary,
    },
    "get_insights": {
        "desc": (
            "Period-over-period comparison (vs previous month/year), 12-month category trends, "
            "and month-end forecast. scope: 'month'|'year'; metric: 'expense'|'income'."
        ),
        "args": {"scope": "month", "metric": "expense", "period": None, "ledger_id": None},
        "fn": read_tools.get_financial_insights,
    },
    "get_budgets": {
        "desc": "Budget list with current-period spent / remaining / percent_used / exceeded.",
        "args": {"ledger_id": None},
        "fn": read_tools.list_budgets,
    },
    "get_net_worth": {
        "desc": "Monthly net-worth history (assets / liabilities, converted to primary currency), last 12 months.",
        "args": {"ledger_id": None},
        "fn": _tool_get_net_worth,
    },
    "list_transactions": {
        "desc": (
            "Search recent transactions (newest first). Optional filters: q (fuzzy note/category/account), "
            "tx_type 'income'|'expense'|'transfer', date_from/date_to 'YYYY-MM-DD', limit<=50."
        ),
        "args": {"q": None, "tx_type": None, "date_from": None, "date_to": None, "limit": 20, "ledger_id": None},
        "fn": read_tools.list_transactions,
    },
}


# 这三个工具支持 tz_offset_minutes;由 server 从请求注入(LLM 不感知),
# 保证模型看到的"本月"跟用户本地时区一致
_TZ_AWARE_TOOLS = {"get_analytics", "get_insights", "get_net_worth"}


async def _run_tool(user: User, name: str, args: dict, *, tz_offset_minutes: int = 0) -> dict:
    """执行工具并把异常转成 error 观测值(Agent 自纠错),不炸 SSE。"""
    spec = _TOOL_SPECS.get(name)
    if spec is None:
        return {"error": f"unknown tool {name!r}; available: {sorted(_TOOL_SPECS)}"}
    cleaned: dict = {}
    for key, value in (args or {}).items():
        if key in spec["args"] and value is not None:
            cleaned[key] = value
    if name in _TZ_AWARE_TOOLS:
        cleaned["tz_offset_minutes"] = tz_offset_minutes
    if name == "list_transactions":
        try:
            cleaned["limit"] = max(1, min(int(cleaned.get("limit", 20)), 50))
        except (TypeError, ValueError):
            cleaned["limit"] = 20
    try:
        # read_tools 内部各自开 SessionLocal(SQLAlchemy sync),丢线程池跑
        result = await asyncio.to_thread(spec["fn"], user, **cleaned)
        return result if isinstance(result, (dict, list)) else {"value": result}
    except Exception as exc:  # noqa: BLE001 — 工具失败=观测值,交给模型兜
        logger.warning("ai.agent tool error user=%s tool=%s: %s", user.id, name, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


# ──────────────── prompt ────────────────


def _build_system_prompt(*, lang: str, today: str, tz_label: str) -> str:
    tool_lines = "\n".join(
        f'- {name}: {spec["desc"]}' for name, spec in _TOOL_SPECS.items()
    )
    if lang == "en":
        return (
            "You are BeeCount's financial assistant embedded in the user's self-hosted web console. "
            f"Today is {today} ({tz_label}).\n"
            "You may ONLY see the user's data through the tools below — never invent numbers. "
            "For any question about the user's finances, call a tool first; if the results don't "
            "answer it, call another tool (at most a few rounds), then answer.\n"
            "Available tools:\n" + tool_lines + "\n\n"
            "Reply with ONE JSON object and nothing else:\n"
            '  {"action": "tool", "tool": "<name>", "args": {...}}   — to call a tool\n'
            '  {"action": "answer", "answer": "<final reply>"}        — when you can answer\n'
            "Answer style: concise, structured (short paragraphs / '-' bullet lists / **bold** key figures), "
            "mention currency, compare periods when useful. Financial advice must be practical, "
            "data-grounded, and clearly labeled as reference, not professional investment advice.\n"
            "If the user asks something unrelated to their finances, answer briefly and steer back."
        )
    return (
        "你是 BeeCount 自托管记账云的财务助手,嵌在用户的 Web 控制台里。"
        f"今天是 {today}({tz_label})。\n"
        "你只能通过下面的工具查看用户数据 —— 严禁编造任何数字。凡涉及用户账目的问题,"
        "先调用工具;一个工具不够就再调(总共不超过几轮),然后作答。\n"
        "可用工具:\n" + tool_lines + "\n\n"
        "每次只回复一个 JSON 对象,不要输出其他内容:\n"
        '  {"action": "tool", "tool": "<名称>", "args": {...}}   — 调用工具\n'
        '  {"action": "answer", "answer": "<最终回答>"}          — 可以作答时\n'
        "回答风格:简洁、结构化(短段落 / '-' 列表 / **加粗**关键数字),给出币种,"
        "有对比价值时做环比/同比。财务建议要具体、基于数据,并注明仅供参考,"
        "不构成专业投资建议。与记账无关的问题简短回答后引导回财务话题。"
    )


def _sse(event_type: str, data: object) -> str:
    return f"data: {json.dumps({'type': event_type, **(data if isinstance(data, dict) else {'value': data})}, ensure_ascii=False)}\n\n"


# ──────────────── endpoint ────────────────


@router.post("/agent/chat")
async def agent_chat(
    req: AgentChatRequest,
    _scopes: set[str] = Depends(_AGENT_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 1) 速率限制 — Agent 一次请求触发多次 LLM 调用,更紧的窗口
    if not _check_rate_limit(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error_code": "AI_AGENT_RATE_LIMITED"},
        )

    # 2) 解析 chat provider — 没配直接 400(前端引导去 AI 设置页)
    profile = db.scalar(select(UserProfile).where(UserProfile.user_id == current_user.id))
    try:
        provider_cfg = resolve_chat_provider(current_user, profile)
    except NoChatProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "AI_NO_CHAT_PROVIDER", "message": str(exc)},
        ) from exc

    # 3) 组装对话:system + 客户端历史(截尾,只留 user/assistant)
    lang = "en" if req.locale == "en" else "zh"
    local_now = datetime.now(_tz.utc) + timedelta(minutes=req.tz_offset_minutes)
    offset_hours = req.tz_offset_minutes / 60
    tz_label = f"UTC{offset_hours:+g}"
    history = [
        {"role": m.role, "content": m.content} for m in req.messages[-_MAX_HISTORY:]
    ]
    if not history or history[-1]["role"] != "user":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "AI_AGENT_INVALID_MESSAGES", "message": "last message must be user"},
        )

    messages: list[dict] = [
        {
            "role": "system",
            "content": _build_system_prompt(
                lang=lang, today=local_now.strftime("%Y-%m-%d"), tz_label=tz_label
            ),
        },
        *history,
    ]

    logger.info(
        "ai.agent chat user=%s lang=%s msgs=%d provider=%s",
        current_user.id, req.locale, len(history), provider_cfg.provider_id,
    )

    async def stream() -> AsyncIterator[bytes]:
        tool_trace: list[str] = []
        consecutive_invalid = 0
        answer: str | None = None
        try:
            for _round in range(1, _MAX_TOOL_ROUNDS + 1):
                result = await call_chat_json(
                    config=provider_cfg, messages=messages, timeout=45.0,
                )
                if not isinstance(result, dict):
                    result = {"raw": result}
                answer = result.get("answer")
                tool_name = result.get("tool") or result.get("tool_name")
                action = result.get("action")

                if (action == "answer" or (action is None and answer)) and isinstance(answer, str) and answer.strip():
                    break
                if action != "tool" or not isinstance(tool_name, str):
                    # 协议外输出:纠偏一次;连续两次失败按能力不足兜底报错
                    consecutive_invalid += 1
                    if consecutive_invalid >= 2:
                        yield _sse("error", {
                            "error_code": "AI_AGENT_INVALID_OUTPUT",
                            "message": "model did not follow the agent protocol",
                        }).encode("utf-8")
                        return
                    messages.append({"role": "user", "content": (
                        "INVALID_OUTPUT: reply with a single JSON object "
                        '{"action":"tool","tool":...,"args":{...}} or {"action":"answer","answer":...}.'
                    )})
                    continue
                consecutive_invalid = 0

                args = result.get("args") if isinstance(result.get("args"), dict) else {}
                yield _sse("status", {"tool": tool_name, "phase": "start"}).encode("utf-8")
                tool_result = await _run_tool(
                    current_user, tool_name, args, tz_offset_minutes=req.tz_offset_minutes,
                )
                if "error" not in tool_result:
                    tool_trace.append(tool_name)
                yield _sse("status", {"tool": tool_name, "phase": "done"}).encode("utf-8")

                result_text = json.dumps(tool_result, ensure_ascii=False, default=str)
                if len(result_text) > _MAX_TOOL_RESULT_CHARS:
                    result_text = result_text[:_MAX_TOOL_RESULT_CHARS] + "…(truncated)"
                messages.append({"role": "assistant", "content": json.dumps(
                    {"action": "tool", "tool": tool_name, "args": args}, ensure_ascii=False,
                )})
                messages.append({"role": "user", "content": f"TOOL_RESULT {tool_name}: {result_text}"})
            else:
                # 轮次用尽:强制直接作答(不再给工具选项)
                messages.append({"role": "user", "content": (
                    "TOOL_ROUNDS_EXHAUSTED: no more tool calls. Answer now with "
                    '{"action":"answer","answer":...} using the data you have.'
                )})
                result = await call_chat_json(
                    config=provider_cfg, messages=messages, timeout=45.0,
                )
                answer = result.get("answer") if isinstance(result, dict) else None

            if not (isinstance(answer, str) and answer.strip()):
                yield _sse("error", {
                    "error_code": "AI_AGENT_INVALID_OUTPUT",
                    "message": "model returned no answer",
                }).encode("utf-8")
                return

            yield _sse("chunk", {"text": answer}).encode("utf-8")
            if tool_trace:
                yield _sse("tool_calls", {"items": [{"name": n} for n in tool_trace]}).encode("utf-8")
            yield _sse("done", {"rounds": len(tool_trace)}).encode("utf-8")
            logger.info(
                "ai.agent done user=%s tools=%s", current_user.id, tool_trace,
            )
        except ChatProviderError as exc:
            logger.warning("ai.agent provider error user=%s: %s", current_user.id, exc)
            yield _sse("error", {
                "error_code": "AI_PROVIDER_ERROR", "message": str(exc)[:200],
            }).encode("utf-8")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
