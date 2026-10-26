"""财务洞察端点:/workspace/insights。

一个端点喂饱 Web「图表分析」页的三块能力(此前 web 端完全没有的增量分析):
  1. comparison      — 本期 vs 上期(月/年)收支对比 + 环比 delta + 储蓄率
  2. category_trends — 最近 12 个月 Top 分类月度时序(堆叠趋势图数据)
  3. forecast        — 本月收支预测:已发生 + 剩余天数按「近 3 个完整月日均」
                       推算月末(pace),无历史时退回本月 run-rate

口径与 workspace_analytics 逐位一致:账本维度折本位币(native_amount ?? amount)、
exclude_from_stats 过滤、tz_offset_minutes 本地时区切月、month_start_day 周期
(起始日账本的"月"按其周期边界切)。单次 SQL 拉窗口内明细在 Python 分桶
(个人账本 13 个月明细,与 analytics scope=year 同量级),三块能力共用一趟扫描。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from sqlalchemy import false as sa_false

from ._shared import *  # noqa: F401,F403 — imports + helpers + router

# ---------------------------------------------------------------------------
# 响应 schema(net-worth-history 同款:模型本地定义,不动全局 schemas.py)
# ---------------------------------------------------------------------------

InsightsScope = Literal["month", "year"]
InsightsMetric = Literal["expense", "income"]

# 分类趋势最多拆出 Top N 个分类,其余并入 "other" 行(前端翻译成「其他」)。
_TREND_TOP_CATEGORIES = 6
# 趋势窗口:最近 12 个周期月(含当前月)。
_TREND_MONTHS = 12
# 预测的 trailing 基线:最近 3 个**已发生**(有交易)的完整周期月。
_FORECAST_TRAILING_MONTHS = 3
# 预测卡片的上下文图:最近 N 个完整月 + 当前月。
_FORECAST_HISTORY_MONTHS = 6


class InsightsPeriodSummaryOut(BaseModel):
    period: str
    income_total: float
    expense_total: float
    balance: float
    transaction_count: int
    # (income-expense)/income × 100;income<=0 时为 None(无意义,前端显示 "-")
    savings_rate: float | None


class InsightsDeltasOut(BaseModel):
    """环比 delta。prev 为 0 时 pct 无意义 → None(前端显示「新增/无数据」)。"""

    income_pct: float | None
    expense_pct: float | None
    balance_abs: float
    # 储蓄率变化,单位百分点;任一期储蓄率 None 时为 None
    savings_rate_pct: float | None


class InsightsComparisonOut(BaseModel):
    scope: InsightsScope
    current: InsightsPeriodSummaryOut
    previous: InsightsPeriodSummaryOut
    deltas: InsightsDeltasOut


class InsightsCategoryTrendOut(BaseModel):
    category_name: str
    # "其他"聚合行:category_name 留空,前端用 i18n 键渲染
    is_other: bool = False
    total: float
    # 与 months 对齐的月度金额(缺月为 0)
    series: list[float]


class InsightsCategoryTrendsOut(BaseModel):
    metric: InsightsMetric
    months: list[str]
    categories: list[InsightsCategoryTrendOut]


class InsightsForecastOut(BaseModel):
    """本月收支预测 —— 永远针对**当前**周期月计算(与用户选中的对比期无关,
    前端在选中期 == 当前期时才展示该卡片)。"""

    period: str
    days_elapsed: int
    days_total: int
    days_remaining: int
    expense_so_far: float
    income_so_far: float
    # 本月日均(已发生部分);月初第一天天没过完时也照算,elapsed 兜底 0.5 天
    daily_expense_current: float | None
    # 近 3 个完整已发生月的日均支出(预测基线);无历史时 None
    daily_expense_trailing: float | None
    projected_expense: float | None
    projected_income: float | None
    projected_balance: float | None
    projected_savings_rate: float | None
    # pace = 已发生 + trailing 日均 × 剩余天数;run_rate = 本月日均线性外推;
    # no_data = 本月还没数据。
    basis: Literal["pace", "run_rate", "no_data"]
    # 上下文:最近 6 个完整月 + 当前月(含当月至今,末位 bucket == period)
    history_months: list[WorkspaceAnalyticsSeriesItemOut]


class WorkspaceInsightsOut(BaseModel):
    comparison: InsightsComparisonOut
    category_trends: InsightsCategoryTrendsOut
    forecast: InsightsForecastOut


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _shift_month_label(label: str, months: int) -> str:
    """周期月标签平移:"2026-01" -1 → "2025-12"(跨年安全)。"""
    year, month = int(label[:4]), int(label[5:7])
    idx = year * 12 + (month - 1) + months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _month_labels_of_year(year_label: str) -> list[str]:
    """年度周期 "2026"(起始日口径)对应的 12 个周期月标签 2026-01..2026-12。"""
    year = int(year_label[:4])
    return [f"{year:04d}-{month:02d}" for month in range(1, 13)]


def _savings_rate(income: float, expense: float) -> float | None:
    if income <= 0:
        return None
    return (income - expense) / income * 100.0


def _pct_delta(cur: float, prev: float) -> float | None:
    if prev <= 0:
        return None
    return (cur - prev) / prev * 100.0


@router.get("/workspace/insights", response_model=WorkspaceInsightsOut)
def workspace_insights(
    scope: InsightsScope = Query(default="month"),
    metric: InsightsMetric = Query(default="expense"),
    period: str | None = Query(default=None),
    ledger_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    tz_offset_minutes: int = Query(default=0, ge=-720, le=840),
    natural_month: bool = Query(default=False),
    _scopes: set[str] = Depends(_READ_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceInsightsOut:
    is_admin = _is_admin(current_user)
    ledgers = _visible_workspace_ledgers(
        db, current_user=current_user, is_admin=is_admin,
        ledger_id=ledger_id, user_id=user_id,
    )
    if not ledgers:
        return WorkspaceInsightsOut(
            comparison=InsightsComparisonOut(
                scope=scope,
                current=InsightsPeriodSummaryOut(
                    period="", income_total=0.0, expense_total=0.0,
                    balance=0.0, transaction_count=0, savings_rate=None,
                ),
                previous=InsightsPeriodSummaryOut(
                    period="", income_total=0.0, expense_total=0.0,
                    balance=0.0, transaction_count=0, savings_rate=None,
                ),
                deltas=InsightsDeltasOut(
                    income_pct=None, expense_pct=None,
                    balance_abs=0.0, savings_rate_pct=None,
                ),
            ),
            category_trends=InsightsCategoryTrendsOut(
                metric=metric, months=[], categories=[],
            ),
            forecast=InsightsForecastOut(
                period="", days_elapsed=0, days_total=0, days_remaining=0,
                expense_so_far=0.0, income_so_far=0.0,
                daily_expense_current=None, daily_expense_trailing=None,
                projected_expense=None, projected_income=None,
                projected_balance=None, projected_savings_rate=None,
                basis="no_data", history_months=[],
            ),
        )

    # 单账本用其自定义起始日;多账本聚合维持自然月(与 workspace_analytics 同规)
    month_start_day = (
        1
        if natural_month
        else ((ledgers[0].month_start_day or 1) if len(ledgers) == 1 else 1)
    )

    # --- 确定三块能力各自的周期窗口 -------------------------------------
    # 1) 对比:选中期 + 上一期(scope=month|year)
    sel_start, sel_end, sel_label = _analytics_range(
        scope=scope, period=period, tz_offset_minutes=tz_offset_minutes,
        month_start_day=month_start_day,
    )
    if scope == "year":
        prev_label = f"{int(sel_label[:4]) - 1:04d}"
    else:
        prev_label = _shift_month_label(sel_label, -1)
    prev_start, prev_end, _ = _analytics_range(
        scope=scope, period=prev_label, tz_offset_minutes=tz_offset_minutes,
        month_start_day=month_start_day,
    )
    # 2) 分类趋势:最近 12 个周期月(以**当前**周期月收尾,与用户选期无关 —
    #    趋势图的价值在"最近",跟对比卡选了哪个月解耦)
    cur_start, cur_end, cur_label = _analytics_range(
        scope="month", period=None, tz_offset_minutes=tz_offset_minutes,
        month_start_day=month_start_day,
    )
    trend_first_label = _shift_month_label(cur_label, -(_TREND_MONTHS - 1))
    trend_start, _, _ = _analytics_range(
        scope="month", period=trend_first_label, tz_offset_minutes=tz_offset_minutes,
        month_start_day=month_start_day,
    )
    # SQL 窗口 = 三个窗口的并集 [min(start), max(end))
    window_start = min(sel_start, prev_start, trend_start)
    window_end = max(sel_end, cur_end)

    # --- 单趟扫描,按周期月标签分桶 --------------------------------------
    # month_totals:bucket → {income, expense, count};count 含转账(与
    # analytics 的 transaction_count 同口径),收支金额不含。
    month_totals: dict[str, dict[str, float]] = {}
    cat_by_month: dict[str, dict[str, float]] = {}
    cat_income_by_month: dict[str, dict[str, float]] = {}
    ledger_internal_ids = [lg.id for lg in ledgers]
    if ledger_internal_ids:
        tx_query = select(
            ReadTxProjection.tx_type,
            func.coalesce(ReadTxProjection.native_amount, ReadTxProjection.amount),
            ReadTxProjection.happened_at,
            ReadTxProjection.category_name,
        ).where(
            ReadTxProjection.ledger_id.in_(ledger_internal_ids),
            # 与 workspace_analytics 同口径:收支统计排除标记笔(D1)
            ReadTxProjection.exclude_from_stats == sa_false(),
            ReadTxProjection.happened_at >= window_start,
            ReadTxProjection.happened_at < window_end,
        )
        for tx_type_val, amount, happened_at_raw, cat_name in db.execute(tx_query).all():
            if happened_at_raw is None:
                continue
            happened_at = _to_utc(happened_at_raw)
            amt = float(amount or 0.0)
            bucket = _bucket_key("year", happened_at, tz_offset_minutes, month_start_day)
            slot = month_totals.setdefault(
                bucket, {"income": 0.0, "expense": 0.0, "count": 0.0}
            )
            slot["count"] += 1.0
            if tx_type_val == "income":
                slot["income"] += amt
            elif tx_type_val == "expense":
                slot["expense"] += amt
            else:
                continue
            category = (cat_name or "").strip() or "Uncategorized"
            target = cat_by_month if tx_type_val == "expense" else cat_income_by_month
            month_cats = target.setdefault(bucket, {})
            month_cats[category] = month_cats.get(category, 0.0) + amt

    # --- 1) comparison ---------------------------------------------------
    def _period_summary(label: str, months: list[str]) -> InsightsPeriodSummaryOut:
        income = sum(month_totals.get(m, {}).get("income", 0.0) for m in months)
        expense = sum(month_totals.get(m, {}).get("expense", 0.0) for m in months)
        count = sum(month_totals.get(m, {}).get("count", 0.0) for m in months)
        return InsightsPeriodSummaryOut(
            period=label,
            income_total=income,
            expense_total=expense,
            balance=income - expense,
            transaction_count=int(count),
            savings_rate=_savings_rate(income, expense),
        )

    if scope == "year":
        cur_months = _month_labels_of_year(sel_label)
        prev_months = _month_labels_of_year(prev_label)
    else:
        cur_months, prev_months = [sel_label], [prev_label]
    current_summary = _period_summary(sel_label, cur_months)
    previous_summary = _period_summary(prev_label, prev_months)
    savings_delta = (
        current_summary.savings_rate - previous_summary.savings_rate
        if current_summary.savings_rate is not None
        and previous_summary.savings_rate is not None
        else None
    )
    comparison = InsightsComparisonOut(
        scope=scope,
        current=current_summary,
        previous=previous_summary,
        deltas=InsightsDeltasOut(
            income_pct=_pct_delta(current_summary.income_total, previous_summary.income_total),
            expense_pct=_pct_delta(current_summary.expense_total, previous_summary.expense_total),
            balance_abs=current_summary.balance - previous_summary.balance,
            savings_rate_pct=savings_delta,
        ),
    )

    # --- 2) category_trends(最近 12 个月,以当前周期月收尾)------------
    trend_labels = [
        _shift_month_label(cur_label, offset)
        for offset in range(-(_TREND_MONTHS - 1), 1)
    ]
    trend_source = cat_by_month if metric == "expense" else cat_income_by_month
    totals_by_cat: dict[str, float] = {}
    for bucket in trend_labels:
        for cat, amount in trend_source.get(bucket, {}).items():
            totals_by_cat[cat] = totals_by_cat.get(cat, 0.0) + amount
    ranked = sorted(
        ((c, a) for c, a in totals_by_cat.items() if a > 0),
        key=lambda x: (-x[1], x[0]),
    )
    top_names = [c for c, _ in ranked[:_TREND_TOP_CATEGORIES]]
    other_total = sum(a for c, a in ranked if c not in top_names)

    def _trend_series(name: str) -> list[float]:
        return [
            float(trend_source.get(bucket, {}).get(name, 0.0))
            for bucket in trend_labels
        ]

    trend_rows: list[InsightsCategoryTrendOut] = [
        InsightsCategoryTrendOut(
            category_name=name,
            total=totals_by_cat.get(name, 0.0),
            series=_trend_series(name),
        )
        for name in top_names
    ]
    if other_total > 0:
        trend_rows.append(InsightsCategoryTrendOut(
            category_name="",
            is_other=True,
            total=other_total,
            series=[
                sum(
                    amount for cat, amount in trend_source.get(bucket, {}).items()
                    if cat not in top_names
                )
                for bucket in trend_labels
            ],
        ))
    category_trends = InsightsCategoryTrendsOut(
        metric=metric, months=trend_labels, categories=trend_rows,
    )

    # --- 3) forecast(永远针对当前周期月)--------------------------------
    days_total = max(1, (cur_end - cur_start).days)
    now = datetime.now(timezone.utc)
    elapsed = (now - cur_start).total_seconds() / 86400.0
    elapsed = min(max(elapsed, 0.0), float(days_total))
    days_elapsed = min(days_total, int(elapsed) + 1)
    days_remaining = max(0, days_total - days_elapsed)

    cur_slot = month_totals.get(cur_label, {})
    expense_so_far = float(cur_slot.get("expense", 0.0))
    income_so_far = float(cur_slot.get("income", 0.0))

    # trailing 基线:近 3 个完整周期月里「已发生」(有交易)的那些
    trailing_labels = [
        _shift_month_label(cur_label, -offset)
        for offset in range(_FORECAST_TRAILING_MONTHS, 0, -1)
    ]
    trailing_expense = 0.0
    trailing_days = 0.0
    for lab in trailing_labels:
        slot = month_totals.get(lab)
        if not slot or slot.get("count", 0.0) <= 0:
            continue
        lab_start, lab_end, _ = _analytics_range(
            scope="month", period=lab, tz_offset_minutes=tz_offset_minutes,
            month_start_day=month_start_day,
        )
        trailing_expense += float(slot.get("expense", 0.0))
        trailing_days += max(1, (lab_end - lab_start).days)

    daily_trailing: float | None = (
        trailing_expense / trailing_days if trailing_days > 0 else None
    )
    daily_current: float | None = (
        expense_so_far / max(elapsed, 0.5) if expense_so_far > 0 else None
    )

    remaining_float = max(0.0, float(days_total) - elapsed)
    if daily_trailing is not None:
        basis: Literal["pace", "run_rate", "no_data"] = "pace"
        projected_expense = expense_so_far + daily_trailing * remaining_float
        projected_income = income_so_far + (
            sum(
                month_totals.get(lab, {}).get("income", 0.0) for lab in trailing_labels
            ) / trailing_days
        ) * remaining_float
    elif expense_so_far > 0 or income_so_far > 0:
        basis = "run_rate"
        projected_expense = expense_so_far / max(elapsed, 0.5) * days_total
        projected_income = income_so_far / max(elapsed, 0.5) * days_total
    else:
        basis = "no_data"
        projected_expense = 0.0
        projected_income = 0.0
    # 预测永远不低于已发生(月末只会多不会少)
    projected_expense = max(projected_expense, expense_so_far)
    projected_income = max(projected_income, income_so_far)
    projected_balance = projected_income - projected_expense

    history_labels = [
        _shift_month_label(cur_label, -offset)
        for offset in range(_FORECAST_HISTORY_MONTHS, -1, -1)
    ]
    history_months = [
        WorkspaceAnalyticsSeriesItemOut(
            bucket=lab,
            expense=float(month_totals.get(lab, {}).get("expense", 0.0)),
            income=float(month_totals.get(lab, {}).get("income", 0.0)),
            balance=(
                float(month_totals.get(lab, {}).get("income", 0.0))
                - float(month_totals.get(lab, {}).get("expense", 0.0))
            ),
        )
        for lab in history_labels
    ]

    forecast = InsightsForecastOut(
        period=cur_label,
        days_elapsed=days_elapsed,
        days_total=days_total,
        days_remaining=days_remaining,
        expense_so_far=expense_so_far,
        income_so_far=income_so_far,
        daily_expense_current=daily_current,
        daily_expense_trailing=daily_trailing,
        projected_expense=projected_expense,
        projected_income=projected_income,
        projected_balance=projected_balance,
        projected_savings_rate=_savings_rate(projected_income, projected_expense),
        basis=basis,
        history_months=history_months,
    )

    return WorkspaceInsightsOut(
        comparison=comparison,
        category_trends=category_trends,
        forecast=forecast,
    )
