"""财务洞察端点 /read/workspace/insights:

- comparison:本期 vs 上期收支对比(月/年)+ 环比 delta + 储蓄率
- category_trends:最近 12 个周期月的 Top 分类月度时序
- forecast:当前周期月的收支预测(pace / run_rate / no_data)

口径跟 workspace_analytics 对齐:exclude_from_stats 过滤、native_amount ?? amount、
转账计入 transaction_count 但不计收支。fixture 与 test_net_worth_history.py 同套。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app


def _make_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app), TS


def _iso(dt=None):
    return (dt or datetime.now(timezone.utc)).isoformat()


def _register_and_token(client: TestClient, email: str, *, device_id: str, client_type: str) -> str:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "Pa$$word1!",
            "device_id": device_id,
            "client_type": client_type,
            "device_name": f"pytest-{client_type}",
            "platform": "test",
        },
    )
    r = client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "password": "Pa$$word1!",
            "device_id": device_id,
            "client_type": client_type,
            "device_name": f"pytest-{client_type}",
            "platform": "test",
        },
    )
    return r.json()["access_token"]


def _two_tokens(client, email):
    app_token = _register_and_token(client, email, device_id="d-app", client_type="app")
    web_token = _register_and_token(client, email, device_id="d-web", client_type="web")
    return app_token, web_token


def _push(client, hdr, ledger_id, entity_type, sync_id, payload, *, action="upsert"):
    body = {
        "ledger_id": ledger_id,
        "entity_type": entity_type,
        "entity_sync_id": sync_id,
        "action": action,
        "updated_at": _iso(),
        "payload": payload,
    }
    r = client.post(
        "/api/v1/sync/push",
        headers=hdr,
        json={"device_id": "d-app", "changes": [body]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _shift_month(label: str, months: int) -> str:
    year, month = int(label[:4]), int(label[5:7])
    idx = year * 12 + (month - 1) + months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def test_insights_month_comparison_trends_and_forecast():
    """本月:支出 300(餐饮)+ 收入 1000(工资)+ 转账 50;上月:支出 200 + 收入 800;
    上上月:支出 100。验证对比 delta、储蓄率、12 个月分类趋势对齐、预测 so_far。"""
    client, _ = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "ins1@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}

        _push(client, hdr_app, "lg1", "ledger", "lg1",
              {"syncId": "lg1", "ledgerName": "个人账本", "currency": "CNY"})

        now = datetime.now(timezone.utc)
        cur_label = now.strftime("%Y-%m")
        prev_label = _shift_month(cur_label, -1)
        # 月中锚点:本月用 now(必然落在当前桶),历史月用「当月 1 号 - 5 天」
        # (必然落在上一个标签月,不受测试运行日影响)
        prev_mid = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0) - timedelta(days=5)
        prev_prev_mid = prev_mid - timedelta(days=30)

        # 本月:餐饮 300 + 工资 1000 + 转账 50
        _push(client, hdr_app, "lg1", "transaction", "tx-cur-exp",
              {"syncId": "tx-cur-exp", "type": "expense", "amount": 300,
               "categoryName": "餐饮", "happenedAt": _iso(now)})
        _push(client, hdr_app, "lg1", "transaction", "tx-cur-inc",
              {"syncId": "tx-cur-inc", "type": "income", "amount": 1000,
               "categoryName": "工资", "happenedAt": _iso(now)})
        _push(client, hdr_app, "lg1", "transaction", "tx-cur-tr",
              {"syncId": "tx-cur-tr", "type": "transfer", "amount": 50,
               "fromAccountId": "acc-a", "toAccountId": "acc-b",
               "happenedAt": _iso(now)})
        # 上月:餐饮 200 + 工资 800
        _push(client, hdr_app, "lg1", "transaction", "tx-prev-exp",
              {"syncId": "tx-prev-exp", "type": "expense", "amount": 200,
               "categoryName": "餐饮", "happenedAt": _iso(prev_mid)})
        _push(client, hdr_app, "lg1", "transaction", "tx-prev-inc",
              {"syncId": "tx-prev-inc", "type": "income", "amount": 800,
               "categoryName": "工资", "happenedAt": _iso(prev_mid)})
        # 上上月:餐饮 100(让 trailing 基线有历史)
        _push(client, hdr_app, "lg1", "transaction", "tx-prev2-exp",
              {"syncId": "tx-prev2-exp", "type": "expense", "amount": 100,
               "categoryName": "餐饮", "happenedAt": _iso(prev_prev_mid)})

        r = client.get(
            "/api/v1/read/workspace/insights",
            headers=hdr_web,
            params={"scope": "month", "metric": "expense", "period": cur_label},
        )
        assert r.status_code == 200, r.text
        body = r.json()

        # --- comparison ---
        comparison = body["comparison"]
        assert comparison["current"]["period"] == cur_label
        assert comparison["previous"]["period"] == prev_label
        assert comparison["current"]["expense_total"] == 300.0
        assert comparison["current"]["income_total"] == 1000.0
        assert comparison["current"]["balance"] == 700.0
        # 转账计入 transaction_count 但不计收支(与 analytics 同口径)
        assert comparison["current"]["transaction_count"] == 3
        assert comparison["current"]["savings_rate"] == 70.0
        assert comparison["previous"]["expense_total"] == 200.0
        assert comparison["previous"]["income_total"] == 800.0
        assert comparison["previous"]["transaction_count"] == 2
        assert comparison["previous"]["savings_rate"] == 75.0
        deltas = comparison["deltas"]
        assert deltas["income_pct"] == 25.0
        assert deltas["expense_pct"] == 50.0
        assert deltas["balance_abs"] == 100.0
        assert deltas["savings_rate_pct"] == -5.0

        # --- category_trends:12 个月窗口以当前月收尾,金额按月对齐 ---
        trends = body["category_trends"]
        assert len(trends["months"]) == 12
        assert trends["months"][-1] == cur_label
        assert trends["months"][-2] == prev_label
        by_name = {row["category_name"]: row for row in trends["categories"]}
        food = by_name["餐饮"]
        assert food["total"] == 600.0
        assert food["series"][-1] == 300.0
        assert food["series"][-2] == 200.0
        assert food["series"][-3] == 100.0
        assert len(food["series"]) == 12

        # --- forecast:针对当前周期月 ---
        forecast = body["forecast"]
        assert forecast["period"] == cur_label
        assert forecast["expense_so_far"] == 300.0
        assert forecast["income_so_far"] == 1000.0
        # 上上月有历史 → pace 基线;预测不低于已发生
        assert forecast["basis"] == "pace"
        assert forecast["projected_expense"] >= 300.0
        assert 28 <= forecast["days_total"] <= 31
        assert 1 <= forecast["days_elapsed"] <= forecast["days_total"]
        assert forecast["days_remaining"] == forecast["days_total"] - forecast["days_elapsed"]
        assert forecast["daily_expense_trailing"] is not None and forecast["daily_expense_trailing"] > 0
        # 上下文图:最近 6 个完整月 + 当前月
        assert len(forecast["history_months"]) == 7
        assert forecast["history_months"][-1]["bucket"] == cur_label
        assert forecast["history_months"][-1]["expense"] == 300.0
    finally:
        app.dependency_overrides.clear()


def test_insights_income_metric_trends():
    """metric=income 时分类趋势按收入口径聚合。"""
    client, _ = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "ins2@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}

        _push(client, hdr_app, "lg1", "ledger", "lg1",
              {"syncId": "lg1", "ledgerName": "个人账本", "currency": "CNY"})
        now = datetime.now(timezone.utc)
        _push(client, hdr_app, "lg1", "transaction", "tx-inc",
              {"syncId": "tx-inc", "type": "income", "amount": 900,
               "categoryName": "工资", "happenedAt": _iso(now)})
        _push(client, hdr_app, "lg1", "transaction", "tx-exp",
              {"syncId": "tx-exp", "type": "expense", "amount": 777,
               "categoryName": "餐饮", "happenedAt": _iso(now)})

        r = client.get(
            "/api/v1/read/workspace/insights",
            headers=hdr_web,
            params={"scope": "month", "metric": "income"},
        )
        assert r.status_code == 200, r.text
        trends = r.json()["category_trends"]
        names = [row["category_name"] for row in trends["categories"]]
        assert "工资" in names
        assert "餐饮" not in names
    finally:
        app.dependency_overrides.clear()


def test_insights_year_scope_comparison():
    """年 scope:对比整年(12 个周期月求和)vs 上一年。"""
    client, _ = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "ins3@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}

        _push(client, hdr_app, "lg1", "ledger", "lg1",
              {"syncId": "lg1", "ledgerName": "个人账本", "currency": "CNY"})
        now = datetime.now(timezone.utc)
        year = now.year
        for sid, month, amount in [
            ("tx-jan", 1, 100.0), ("tx-feb", 2, 50.0),
        ]:
            _push(client, hdr_app, "lg1", "transaction", sid,
                  {"syncId": sid, "type": "expense", "amount": amount,
                   "categoryName": "餐饮",
                   "happenedAt": _iso(datetime(year, month, 15, tzinfo=timezone.utc))})
        _push(client, hdr_app, "lg1", "transaction", "tx-last-year",
              {"syncId": "tx-last-year", "type": "expense", "amount": 500,
               "categoryName": "餐饮",
               "happenedAt": _iso(datetime(year - 1, 6, 15, tzinfo=timezone.utc))})

        r = client.get(
            "/api/v1/read/workspace/insights",
            headers=hdr_web,
            params={"scope": "year", "period": str(year)},
        )
        assert r.status_code == 200, r.text
        comparison = r.json()["comparison"]
        assert comparison["current"]["period"] == f"{year:04d}"
        assert comparison["current"]["expense_total"] == 150.0
        assert comparison["previous"]["period"] == f"{year - 1:04d}"
        assert comparison["previous"]["expense_total"] == 500.0
        # 上年有支出、本年 0 收入 → 上年 savings_rate None(收入 0 无意义)
        assert comparison["previous"]["savings_rate"] is None
    finally:
        app.dependency_overrides.clear()


def test_insights_excludes_flagged_transactions():
    """exclude_from_stats=True 的交易不进对比/趋势/预测(与 analytics D1 同口径)。"""
    client, _ = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "ins4@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}

        _push(client, hdr_app, "lg1", "ledger", "lg1",
              {"syncId": "lg1", "ledgerName": "个人账本", "currency": "CNY"})
        now = datetime.now(timezone.utc)
        _push(client, hdr_app, "lg1", "transaction", "tx-normal",
              {"syncId": "tx-normal", "type": "expense", "amount": 100,
               "categoryName": "餐饮", "happenedAt": _iso(now)})
        _push(client, hdr_app, "lg1", "transaction", "tx-excluded",
              {"syncId": "tx-excluded", "type": "expense", "amount": 999,
               "categoryName": "餐饮", "excludeFromStats": True,
               "happenedAt": _iso(now)})

        r = client.get(
            "/api/v1/read/workspace/insights",
            headers=hdr_web,
            params={"scope": "month", "metric": "expense"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["comparison"]["current"]["expense_total"] == 100.0
        assert body["forecast"]["expense_so_far"] == 100.0
        for row in body["category_trends"]["categories"]:
            assert row["series"][-1] == (100.0 if row["category_name"] == "餐饮" else 0.0)
    finally:
        app.dependency_overrides.clear()


def test_insights_no_ledgers_returns_zero_shapes():
    """无任何账本:返回全零结构(period 空串 / basis no_data),不 500。"""
    client, _ = _make_client()
    try:
        _, web_token = _two_tokens(client, "ins5@t.com")
        hdr_web = {"Authorization": f"Bearer {web_token}"}

        r = client.get(
            "/api/v1/read/workspace/insights",
            headers=hdr_web,
            params={"scope": "month"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["comparison"]["current"]["period"] == ""
        assert body["comparison"]["current"]["expense_total"] == 0.0
        assert body["comparison"]["deltas"]["income_pct"] is None
        assert body["category_trends"]["months"] == []
        assert body["forecast"]["basis"] == "no_data"
        assert body["forecast"]["history_months"] == []
    finally:
        app.dependency_overrides.clear()


def test_insights_invalid_scope_rejected():
    """scope=all 对洞察无意义(没有"上一期"),422 拒绝。"""
    client, _ = _make_client()
    try:
        _, web_token = _two_tokens(client, "ins6@t.com")
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        r = client.get(
            "/api/v1/read/workspace/insights",
            headers=hdr_web,
            params={"scope": "all"},
        )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()
