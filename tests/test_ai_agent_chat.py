"""POST /api/v1/ai/agent/chat 端到端测试。

覆盖:
1. 没配 chat provider → 400 + AI_NO_CHAT_PROVIDER
2. 多轮工具调用:模型先要 get_insights → 工具结果(真实 DB 数据)回灌 →
   模型作答;SSE 事件序 status(start/done) → chunk → tool_calls → done
3. 未知工具名 → error 观测值回灌(不炸流),模型下一轮作答
4. 连续协议外输出 → SSE error AI_AGENT_INVALID_OUTPUT
5. provider 调用失败 → SSE error AI_PROVIDER_ERROR
6. 速率限制 → 429 + AI_AGENT_RATE_LIMITED
7. 最后一条消息不是 user → 422

LLM mock:monkeypatch agent_chat.call_chat_json(脚本化按调用次序返回)。
read_tools.SessionLocal monkeypatch 到测试 DB(test_mcp_tools 同款)。
"""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.mcp.tools import read_tools
from src.models import User, UserProfile
from src.routers.ai import agent_chat

AI_CONFIG = json.dumps({
    "providers": [{
        "id": "mock", "name": "Mock", "apiKey": "sk-test",
        "baseUrl": "https://mock.test/v1", "textModel": "mock-4",
    }],
    "binding": {"textProviderId": "mock"},
})


def _make_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # read_tools 直连 SessionLocal(不走 dep tree),指到测试 DB
    monkeypatch.setattr(read_tools, "SessionLocal", Session)
    agent_chat._RATE_WINDOWS.clear()
    return TestClient(app), Session


def _register_and_seed(client: TestClient, Session, email: str) -> str:
    """注册用户(app + web 两种 client:app 用来 push 数据,web 用来调 agent)
    + 配 AI provider + 一条本月支出(供工具读取)。返回 web token。"""
    tokens = {}
    for device, client_type in (("ag-app", "app"), ("ag-web", "web")):
        client.post(
            "/api/v1/auth/register",
            json={
                "email": email, "password": "Pa$$word1!", "device_id": device,
                "client_type": client_type, "device_name": f"pytest-{client_type}", "platform": "test",
            },
        )
        r = client.post(
            "/api/v1/auth/login",
            json={
                "email": email, "password": "Pa$$word1!", "device_id": device,
                "client_type": client_type, "device_name": f"pytest-{client_type}", "platform": "test",
            },
        )
        tokens[client_type] = r.json()["access_token"]
    token = tokens["web"]

    db = Session()
    user = db.query(User).filter(User.email == email).first()
    db.add(UserProfile(user_id=user.id, ai_config_json=AI_CONFIG))
    db.commit()
    db.close()

    # 账本 + 本月支出(走 sync/push 生成 projection,用 app token)
    hdr = {"Authorization": f"Bearer {tokens['app']}"}

    def push(entity_type, sync_id, payload):
        body = {
            "ledger_id": "lg-ag", "entity_type": entity_type, "entity_sync_id": sync_id,
            "action": "upsert", "updated_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        resp = client.post(
            "/api/v1/sync/push", headers=hdr,
            json={"device_id": "ag-app", "changes": [body]},
        )
        assert resp.status_code == 200, resp.text

    push("ledger", "lg-ag", {"syncId": "lg-ag", "ledgerName": "Agent 测试账本", "currency": "CNY"})
    push("transaction", "tx-1", {
        "syncId": "tx-1", "type": "expense", "amount": 250,
        "categoryName": "餐饮", "happenedAt": datetime.now(timezone.utc).isoformat(),
    })
    return token


def _sse_events(resp) -> list[dict]:
    out = []
    for block in resp.content.decode().split("\n\n"):
        line = next((ln for ln in block.split("\n") if ln.startswith("data:")), None)
        if line:
            out.append(json.loads(line[len("data:"):].strip()))
    return out


def _chat(client, token, messages=None):
    return client.post(
        "/api/v1/ai/agent/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messages": messages or [{"role": "user", "content": "分析一下我的支出"}],
            "locale": "zh-CN",
            "tz_offset_minutes": 480,
        },
    )


def test_agent_chat_requires_provider(monkeypatch):
    client, Session = _make_client(monkeypatch)
    try:
        client.post("/api/v1/auth/register", json={
            "email": "ag-none@t.com", "password": "Pa$$word1!", "device_id": "d",
            "client_type": "web", "device_name": "t", "platform": "t",
        })
        r = client.post("/api/v1/auth/login", json={
            "email": "ag-none@t.com", "password": "Pa$$word1!", "device_id": "d",
            "client_type": "web", "device_name": "t", "platform": "t",
        })
        resp = _chat(client, r.json()["access_token"])
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "AI_NO_CHAT_PROVIDER"
    finally:
        app.dependency_overrides.clear()


def test_agent_chat_tool_round_then_answer(monkeypatch):
    client, Session = _make_client(monkeypatch)
    try:
        token = _register_and_seed(client, Session, "ag-ok@t.com")

        captured_messages: list[list[dict]] = []

        async def fake_call_json(*, config, messages, timeout=30.0, **kw):
            captured_messages.append([dict(m) for m in messages])
            if len(captured_messages) == 1:
                return {"action": "tool", "tool": "get_insights", "args": {"scope": "month"}}
            return {"action": "answer", "answer": "本月支出 250 元,餐饮分类。"}

        monkeypatch.setattr(agent_chat, "call_chat_json", fake_call_json)

        resp = _chat(client, token)
        assert resp.status_code == 200
        events = _sse_events(resp)
        types = [e["type"] for e in events]
        # 事件序:status(start) → status(done) → chunk → tool_calls → done
        assert types == ["status", "status", "chunk", "tool_calls", "done"]
        assert events[0] == {"type": "status", "tool": "get_insights", "phase": "start"}
        assert events[2]["text"] == "本月支出 250 元,餐饮分类。"
        assert events[3]["items"] == [{"name": "get_insights"}]
        assert events[4]["rounds"] == 1

        # 第二轮 prompt 应包含真实工具结果(expense 250 来自 DB)
        second_round = captured_messages[1]
        obs = next(m for m in second_round if str(m.get("content", "")).startswith("TOOL_RESULT"))
        assert '"expense_total": 250' in obs["content"]
        # 第一轮 prompt 含 system(工具清单 + 本地日期)
        system = captured_messages[0][0]["content"]
        assert "get_insights" in system
    finally:
        app.dependency_overrides.clear()


def test_agent_chat_unknown_tool_feeds_error_observation(monkeypatch):
    client, Session = _make_client(monkeypatch)
    try:
        token = _register_and_seed(client, Session, "ag-badtool@t.com")
        calls: list[dict] = []

        async def fake_call_json(*, config, messages, timeout=30.0, **kw):
            calls.append(messages[-1])
            if len(calls) == 1:
                return {"action": "tool", "tool": "no_such_tool", "args": {}}
            return {"action": "answer", "answer": "好的,工具不存在,我直接回答。"}

        monkeypatch.setattr(agent_chat, "call_chat_json", fake_call_json)
        resp = _chat(client, token)
        assert resp.status_code == 200
        events = _sse_events(resp)
        # 未知工具也发 status(start/done)(执行返回 error 观测),模型自纠错后作答
        assert [e["type"] for e in events] == ["status", "status", "chunk", "done"]
        assert "unknown tool" in calls[1]["content"]
    finally:
        app.dependency_overrides.clear()


def test_agent_chat_invalid_output_gives_up_after_two(monkeypatch):
    client, Session = _make_client(monkeypatch)
    try:
        token = _register_and_seed(client, Session, "ag-invalid@t.com")

        async def fake_call_json(*, config, messages, timeout=30.0, **kw):
            return "I am not a JSON envelope at all"

        monkeypatch.setattr(agent_chat, "call_chat_json", fake_call_json)
        resp = _chat(client, token)
        events = _sse_events(resp)
        assert [e["type"] for e in events] == ["error"]
        assert events[0]["error_code"] == "AI_AGENT_INVALID_OUTPUT"
    finally:
        app.dependency_overrides.clear()


def test_agent_chat_provider_error_mid_stream(monkeypatch):
    from src.services.ai import ChatProviderError

    client, Session = _make_client(monkeypatch)
    try:
        token = _register_and_seed(client, Session, "ag-perr@t.com")

        async def fake_call_json(*, config, messages, timeout=30.0, **kw):
            raise ChatProviderError("mock provider 502")

        monkeypatch.setattr(agent_chat, "call_chat_json", fake_call_json)
        resp = _chat(client, token)
        events = _sse_events(resp)
        assert [e["type"] for e in events] == ["error"]
        assert events[0]["error_code"] == "AI_PROVIDER_ERROR"
    finally:
        app.dependency_overrides.clear()


def test_agent_chat_rate_limited(monkeypatch):
    client, Session = _make_client(monkeypatch)
    try:
        token = _register_and_seed(client, Session, "ag-rate@t.com")
        # 塞满该用户窗口(用未来时间戳,永不过期)
        uid_row = Session().query(User).filter(User.email == "ag-rate@t.com").first()
        agent_chat._RATE_WINDOWS[uid_row.id] = deque(
            [float("inf")] * agent_chat._RATE_LIMIT_MAX
        )

        resp = _chat(client, token)
        assert resp.status_code == 429
        assert resp.json()["error_code"] == "AI_AGENT_RATE_LIMITED"
    finally:
        app.dependency_overrides.clear()


def test_agent_chat_requires_user_last_message(monkeypatch):
    client, Session = _make_client(monkeypatch)
    try:
        token = _register_and_seed(client, Session, "ag-last@t.com")
        resp = _chat(client, token, messages=[{"role": "assistant", "content": "hi"}])
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()
