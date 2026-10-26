import type { ReactNode } from 'react'

import type { InsightsComparison } from '@beecount/api-client'
import { Card, CardContent, useT } from '@beecount/ui'
import { Amount } from '@beecount/web-features'

interface Props {
  comparison: InsightsComparison
  currency: string
}

const EPS = 0.05

/**
 * 环比百分比徽章。`invert=true` 用于"越少越好"的指标(支出):
 * 上升 = 坏(expense 色),下降 = 好(income 色);其余指标反过来。
 * pct 为 null(上期为 0,百分比无意义)时渲染「上期无数据」占位。
 */
function PctDeltaBadge({ pct, invert }: { pct: number | null; invert: boolean }) {
  const t = useT()
  if (pct === null) {
    return <span className="text-[11px] text-muted-foreground">{t('insights.compare.noPrev')}</span>
  }
  const up = pct > EPS
  const down = pct < -EPS
  const good = up ? !invert : down ? invert : null
  const cls =
    good === null
      ? 'text-muted-foreground bg-muted'
      : good
        ? 'text-income bg-[rgb(var(--income-rgb)/0.08)]'
        : 'text-expense bg-[rgb(var(--expense-rgb)/0.08)]'
  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[11px] font-semibold tabular-nums ${cls}`}
    >
      {up ? '▲' : down ? '▼' : '—'}
      {`${Math.abs(pct) >= 1000 ? Math.abs(pct).toFixed(0) : Math.abs(pct).toFixed(1)}%`}
    </span>
  )
}

/** 结余的绝对金额变化徽章:±金额,正 = income 色 / 负 = expense 色。 */
function AbsDeltaBadge({ value, currency }: { value: number; currency: string }) {
  if (Math.abs(value) < 0.005) {
    return <span className="rounded-full bg-muted px-1.5 py-0.5 text-[11px] font-semibold">— 0</span>
  }
  const positive = value > 0
  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[11px] font-semibold tabular-nums ${
        positive
          ? 'text-income bg-[rgb(var(--income-rgb)/0.08)]'
          : 'text-expense bg-[rgb(var(--expense-rgb)/0.08)]'
      }`}
    >
      {positive ? '▲' : '▼'}
      <Amount value={Math.abs(value)} currency={currency} size="xs" />
    </span>
  )
}

function MetricCard({
  label,
  children,
  deltaNode,
  prevNode,
  prevPeriod,
}: {
  label: string
  children: ReactNode
  deltaNode: ReactNode
  prevNode: ReactNode
  prevPeriod: string
}) {
  return (
    <Card className="bc-panel">
      <CardContent className="space-y-1.5 p-4">
        <p className="text-xs font-medium text-muted-foreground">{label}</p>
        <div className="flex items-baseline gap-1">{children}</div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
          {deltaNode}
          <span className="inline-flex items-center gap-1">
            {prevNode}
          </span>
        </div>
        <p className="font-mono text-[10px] text-muted-foreground/70">{prevPeriod}</p>
      </CardContent>
    </Card>
  )
}

/**
 * 环比对比卡 — 本期 vs 上期的支出 / 收入 / 结余 / 储蓄率。
 * 数据全部来自 server `/read/workspace/insights` 的 comparison 块,前端只做展示;
 * 支出取 invert 语义(环比上升标红),结余用绝对金额变化而非百分比。
 */
export function ComparisonCards({ comparison, currency }: Props) {
  const t = useT()
  const cur = comparison.current
  const prev = comparison.previous
  const deltas = comparison.deltas
  const vsLabel = `${t('insights.compare.vs')} ${prev.period || '–'}`

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard
        label={t('insights.compare.expense')}
        deltaNode={<PctDeltaBadge pct={deltas.expense_pct} invert />}
        prevNode={
          <>
            {t('insights.compare.prevLabel')}
            <Amount value={prev.expense_total} currency={currency} size="xs" tone="muted" />
          </>
        }
        prevPeriod={vsLabel}
      >
        <Amount value={cur.expense_total} currency={currency} size="2xl" bold animate tone="negative" />
      </MetricCard>
      <MetricCard
        label={t('insights.compare.income')}
        deltaNode={<PctDeltaBadge pct={deltas.income_pct} invert={false} />}
        prevNode={
          <>
            {t('insights.compare.prevLabel')}
            <Amount value={prev.income_total} currency={currency} size="xs" tone="muted" />
          </>
        }
        prevPeriod={vsLabel}
      >
        <Amount value={cur.income_total} currency={currency} size="2xl" bold animate tone="positive" />
      </MetricCard>
      <MetricCard
        label={t('insights.compare.balance')}
        deltaNode={<AbsDeltaBadge value={deltas.balance_abs} currency={currency} />}
        prevNode={
          <>
            {t('insights.compare.prevLabel')}
            <Amount value={prev.balance} currency={currency} size="xs" tone="muted" />
          </>
        }
        prevPeriod={vsLabel}
      >
        <Amount value={cur.balance} currency={currency} size="2xl" bold animate />
      </MetricCard>
      <MetricCard
        label={t('insights.compare.savingsRate')}
        deltaNode={<PctDeltaBadge pct={deltas.savings_rate_pct} invert={false} />}
        prevNode={
          <span className="tabular-nums">
            {t('insights.compare.prevLabel')}
            {prev.savings_rate === null ? '–' : `${prev.savings_rate.toFixed(1)}%`}
          </span>
        }
        prevPeriod={vsLabel}
      >
        {cur.savings_rate === null ? (
          <span className="text-2xl font-bold text-muted-foreground">–</span>
        ) : (
          <>
            <Amount value={cur.savings_rate} size="2xl" bold animate />
            <span className="text-sm font-semibold text-muted-foreground">%</span>
          </>
        )}
      </MetricCard>
    </div>
  )
}
