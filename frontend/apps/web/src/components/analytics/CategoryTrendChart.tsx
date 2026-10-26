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
import type { InsightsCategoryTrends, InsightsMetric } from '@beecount/api-client'
import { Button, Card, CardContent, CardHeader, CardTitle, useLocale, useT } from '@beecount/ui'
import { Amount } from '@beecount/web-features'

import { formatCompactTick } from '../../i18n/format'

interface Props {
  trends: InsightsCategoryTrends
  metric: InsightsMetric
  onMetricChange: (metric: InsightsMetric) => void
}

// 与 HomeMonthCategoryDonut 同一套 BeeCount mobile 调色盘;最后一格给"其他"中性灰
const PALETTE = ['#ef4444', '#f59e0b', '#3b82f6', '#10b981', '#a855f7', '#6366f1']
const OTHER_COLOR = '#94a3b8'

/**
 * 分类趋势 — 最近 12 个月的 Top 分类堆叠柱图。
 *
 * 数据来自 server insights.category_trends(已按总额排好序、聚合好"其他"),
 * 前端只把 `series` 转成 recharts 的行格式;dataKey 用 c0..cN 索引键
 * (分类名可能含空格/特殊字符,不适合直接当 key),名字经 nameByKey 回填。
 */
export function CategoryTrendChart({ trends, metric, onMetricChange }: Props) {
  const t = useT()
  const { locale } = useLocale()
  const chinese = locale.startsWith('zh')

  const rows = trends.categories
  const nameByKey = useMemo(() => {
    const map = new Map<string, string>()
    rows.forEach((row, idx) => {
      map.set(`c${idx}`, row.is_other ? t('insights.trend.other') : row.category_name || t('insights.trend.other'))
    })
    return map
  }, [rows, t])

  const colorByKey = useMemo(() => {
    const map = new Map<string, string>()
    rows.forEach((row, idx) => {
      map.set(`c${idx}`, row.is_other ? OTHER_COLOR : PALETTE[idx % PALETTE.length])
    })
    return map
  }, [rows])

  const data = useMemo(
    () =>
      trends.months.map((bucket, i) => {
        const point: Record<string, number | string> = { bucket }
        rows.forEach((row, idx) => {
          point[`c${idx}`] = row.series[i] ?? 0
        })
        return point
      }),
    [trends.months, rows],
  )

  const hasData = rows.length > 0

  return (
    <Card className="bc-panel overflow-hidden">
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="text-base">{t('insights.trend.title')}</CardTitle>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            {t('insights.trend.subtitle').replace('{count}', String(trends.months.length))}
          </p>
        </div>
        <div className="flex items-center rounded-lg border border-border/60 p-0.5">
          {(['expense', 'income'] as InsightsMetric[]).map((value) => (
            <Button
              key={value}
              variant="ghost"
              size="sm"
              className={`h-7 rounded-md px-2.5 text-xs ${metric === value ? 'bg-primary/10 font-semibold text-primary' : 'text-muted-foreground'}`}
              onClick={() => onMetricChange(value)}
            >
              {t(`insights.trend.metric.${value}`)}
            </Button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        {hasData ? (
          <>
            <div className="h-60">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                  <XAxis
                    dataKey="bucket"
                    tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }}
                    stroke="hsl(var(--border))"
                    tickFormatter={(bucket: string) => bucket.split('-')[1] || bucket}
                    interval={0}
                  />
                  <YAxis
                    tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }}
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
                      const name = nameByKey.get(key) ?? key
                      return [
                        <span key={key} className="font-mono tabular-nums">
                          {Number(value ?? 0).toLocaleString(undefined, {
                            minimumFractionDigits: 0,
                            maximumFractionDigits: 0,
                          })}
                        </span>,
                        <span key={`${key}-label`} className="max-w-[10rem] truncate">{name}</span>,
                      ] as unknown as never
                    }) as unknown as never}
                  />
                  {rows.map((row, idx) => {
                    const key = `c${idx}`
                    return (
                      <Bar
                        key={key}
                        dataKey={key}
                        stackId="cats"
                        fill={colorByKey.get(key) || OTHER_COLOR}
                        radius={idx === rows.length - 1 ? [4, 4, 0, 0] : undefined}
                      />
                    )
                  })}
                </BarChart>
              </ResponsiveContainer>
            </div>
            {/* 图例:色块 + 分类名 + 窗口合计,自绘以显示总额(与月度环图同风格) */}
            <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
              {rows.map((row, idx) => {
                const key = `c${idx}`
                const name = nameByKey.get(key) ?? ''
                return (
                  <span key={key} className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ background: colorByKey.get(key) || OTHER_COLOR }}
                      aria-hidden
                    />
                    <span className="max-w-[7rem] truncate">{name}</span>
                    <Amount value={row.total} size="xs" tone="muted" />
                  </span>
                )
              })}
            </div>
          </>
        ) : (
          <div className="flex h-48 items-center justify-center text-xs text-muted-foreground">
            {t('insights.trend.empty')}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
