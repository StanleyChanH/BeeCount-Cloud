import { useMemo } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import type { InsightsForecast } from '@beecount/api-client'
import { Card, CardContent, CardHeader, CardTitle, useLocale, useT } from '@beecount/ui'
import { Amount } from '@beecount/web-features'

import { formatCompactTick } from '../../i18n/format'

interface Props {
  forecast: InsightsForecast
  currency: string
}

/**
 * 本月收支预测卡 — server `/read/workspace/insights` 的 forecast 块。
 *
 * 左侧:预计月末支出(大数字)+ 已支出进度 + 日均对比 + 预计储蓄率;
 * 右侧:近 6 个月支出柱图,当前月拆成「已发生(实色)+ 预计部分(半透明)」
 * 两段堆叠,一眼看出预测落在历史什么水平。
 *
 * basis 三态:pace(近 3 月日均节奏,最可信)/ run_rate(本月线性外推,
 * 无历史时兜底)/ no_data(本月还没记账)。预测只是推算,卡片文案明示依据。
 */
export function ForecastCard({ forecast, currency }: Props) {
  const t = useT()
  const { locale } = useLocale()
  const chinese = locale.startsWith('zh')

  const projected = forecast.projected_expense
  const soFarRatio =
    projected && projected > 0 ? Math.min(1, forecast.expense_so_far / projected) : 0

  const chartData = useMemo(
    () =>
      forecast.history_months.map((row) => {
        const isCurrent = row.bucket === forecast.period
        const projectedPart = isCurrent
          ? Math.max(0, (projected ?? 0) - forecast.expense_so_far)
          : 0
        return {
          bucket: row.bucket,
          actual: isCurrent ? forecast.expense_so_far : row.expense,
          projectedPart,
          isCurrent,
        }
      }),
    [forecast.history_months, forecast.period, forecast.expense_so_far, projected],
  )

  const hasData = forecast.basis !== 'no_data'

  return (
    <Card className="bc-panel overflow-hidden">
      <CardHeader>
        <CardTitle className="text-base">{t('insights.forecast.title')}</CardTitle>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          {t('insights.forecast.basis.' + forecast.basis)}
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {hasData ? (
          <>
            <div className="space-y-1">
              <p className="text-xs font-medium text-muted-foreground">
                {t('insights.forecast.projectedExpense')}
              </p>
              <div className="flex items-baseline gap-2">
                <Amount
                  value={projected ?? 0}
                  currency={currency}
                  size="3xl"
                  bold
                  animate
                  tone="negative"
                />
                {forecast.projected_savings_rate !== null ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                    {t('insights.forecast.projectedSavings')}
                    <span className="tabular-nums">{forecast.projected_savings_rate.toFixed(1)}%</span>
                  </span>
                ) : null}
              </div>
            </div>

            {/* 已支出 / 预计 进度条:实色 = 已发生,余下 = 预计部分 */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  {t('insights.forecast.soFar')}
                  <Amount value={forecast.expense_so_far} currency={currency} size="xs" />
                </span>
                <span>
                  {t('insights.forecast.remaining')
                    .replace('{days}', String(forecast.days_remaining))}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-[rgb(var(--expense-rgb))] transition-all"
                  style={{ width: `${(soFarRatio * 100).toFixed(1)}%` }}
                />
              </div>
              <div className="flex items-center justify-between text-[10px] text-muted-foreground/70">
                <span className="font-mono">
                  {t('insights.forecast.dayProgress')
                    .replace('{elapsed}', String(forecast.days_elapsed))
                    .replace('{total}', String(forecast.days_total))}
                </span>
                <span className="inline-flex items-center gap-2">
                  <span className="inline-flex items-center gap-1">
                    {t('insights.forecast.dailyCurrent')}
                    {forecast.daily_expense_current !== null ? (
                      <Amount value={forecast.daily_expense_current} currency={currency} size="xs" />
                    ) : ' –'}
                  </span>
                  {forecast.daily_expense_trailing !== null ? (
                    <span className="inline-flex items-center gap-1">
                      {t('insights.forecast.dailyTrailing')}
                      <Amount value={forecast.daily_expense_trailing} currency={currency} size="xs" />
                    </span>
                  ) : null}
                </span>
              </div>
            </div>

            <div className="h-40">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                  <XAxis
                    dataKey="bucket"
                    tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
                    stroke="hsl(var(--border))"
                    tickFormatter={(bucket: string) => bucket.split('-')[1] || bucket}
                    interval={0}
                  />
                  <YAxis
                    width={36}
                    tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
                    stroke="hsl(var(--border))"
                    tickFormatter={(v) =>
                      formatCompactTick(v, { chinese, wanUnit: t('common.unit.10k') })
                    }
                  />
                  <Tooltip
                    contentStyle={{
                      background: 'hsl(var(--popover))',
                      border: '1px solid hsl(var(--border))',
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                    cursor={{ fill: 'hsl(var(--muted) / 0.4)' }}
                    formatter={((value: number, key: string) => {
                      const label = key === 'actual' ? t('insights.forecast.soFarShort') : t('insights.forecast.projectedBar')
                      return [
                        Number(value ?? 0).toLocaleString(undefined, {
                          minimumFractionDigits: 0,
                          maximumFractionDigits: 0,
                        }),
                        label,
                      ] as unknown as never
                    }) as unknown as never}
                  />
                  <Bar dataKey="actual" stackId="f" fill="rgb(var(--expense-rgb))" radius={[0, 0, 0, 0]} />
                  <Bar dataKey="projectedPart" stackId="f" fill="rgb(var(--expense-rgb))" fillOpacity={0.3} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <p className="text-[10px] text-muted-foreground/70">
              {t('insights.forecast.disclaimer')}
            </p>
          </>
        ) : (
          <div className="flex h-48 items-center justify-center text-xs text-muted-foreground">
            {t('insights.forecast.empty')}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
