import { useCallback, useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

import {
  fetchWorkspaceInsights,
  type InsightsMetric,
  type InsightsScope,
  type WorkspaceInsights,
} from '@beecount/api-client'
import { Button, useLocale, useT } from '@beecount/ui'
import { periodLabel } from '@beecount/web-features'

import { CategoryTrendChart } from '../../components/analytics/CategoryTrendChart'
import { ComparisonCards } from '../../components/analytics/ComparisonCards'
import { ForecastCard } from '../../components/analytics/ForecastCard'
import { YearMonthPicker } from '../../components/YearMonthPicker'
import { useAuth } from '../../context/AuthContext'
import { useLedgers } from '../../context/LedgersContext'
import { usePageCache } from '../../context/PageDataCacheContext'
import { useSyncRefresh } from '../../context/SyncSocketContext'

function shiftMonthLabel(label: string, months: number): string {
  const year = Number(label.slice(0, 4))
  const month = Number(label.slice(5, 7))
  const idx = year * 12 + (month - 1) + months
  return `${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, '0')}`
}

function shiftPeriodLabel(label: string, scope: InsightsScope, delta: number): string {
  if (scope === 'year') return String(Number(label) + delta)
  return shiftMonthLabel(label, delta)
}

/** 当前周期标签(起始日口径):月 = periodLabel(msd);年 = 1 月未到起始日归上年。 */
function currentPeriodLabel(scope: InsightsScope, msd: number): string {
  const now = new Date()
  if (scope === 'year') {
    return String(
      now.getMonth() === 0 && now.getDate() < msd ? now.getFullYear() - 1 : now.getFullYear(),
    )
  }
  return periodLabel(now, msd)
}

/**
 * 图表分析页 — Web 端独有的深度分析视图,数据全部来自
 * GET /read/workspace/insights(环比对比 / 12 个月分类趋势 / 本月预测):
 *
 *   - 顶部:月/年 scope 切换 + 期次步进(‹ 期次 ›,复用 YearMonthPicker)
 *   - ComparisonCards:本期 vs 上期 支出/收入/结余/储蓄率 环比
 *   - CategoryTrendChart:Top 分类 12 个月堆叠趋势(支出/收入口径切换)
 *   - ForecastCard:仅当选中期 == 当前月时展示(server 的 forecast 永远
 *     针对当前周期月,选历史期时预测没有意义)
 *
 * 分类趋势固定以当前月收尾(server 口径),不跟期次选择联动。
 */
export function AnalyticsPage() {
  const t = useT()
  const { locale } = useLocale()
  const { token } = useAuth()
  const { activeLedgerId, currency, currentLedger } = useLedgers()

  const [scope, setScope] = useState<InsightsScope>('month')
  const [metric, setMetric] = useState<InsightsMetric>('expense')
  // null = 跟随"当前周期"(server 默认);切换 scope 时重置回当前期
  const [customPeriod, setCustomPeriod] = useState<string | null>(null)

  const msd = Math.max(1, Math.min(28, currentLedger?.month_start_day ?? 1))
  const defaultPeriod = currentPeriodLabel(scope, msd)
  const effectivePeriod = customPeriod ?? defaultPeriod

  const bucket = activeLedgerId || '__none__'
  const cacheKey = `analyticsInsights:${bucket}:${scope}:${effectivePeriod}:${metric}`
  const [insights, setInsights] = usePageCache<WorkspaceInsights | null>(cacheKey, null)

  const load = useCallback(async () => {
    try {
      const data = await fetchWorkspaceInsights(token, {
        scope,
        metric,
        period: effectivePeriod,
        ledgerId: activeLedgerId || undefined,
        tzOffsetMinutes: -new Date().getTimezoneOffset(),
      })
      setInsights(data)
    } catch {
      // 静默降级:保留缓存数据,页面有空态
    }
    // setInsights 是 usePageCache 返回的稳定 setter
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, scope, metric, effectivePeriod, activeLedgerId, cacheKey])

  useEffect(() => {
    // 有缓存(PageDataCache)时先渲染缓存数据,后台刷新覆盖
    void load()
  }, [load])

  // 其它设备/页面写入后 WS 推事件时重拉
  useSyncRefresh(load)

  const switchScope = (next: InsightsScope) => {
    if (next === scope) return
    setScope(next)
    setCustomPeriod(null)
  }

  const stepPeriod = (delta: number) => {
    setCustomPeriod(shiftPeriodLabel(effectivePeriod, scope, delta))
  }

  const isCurrentPeriod = effectivePeriod === defaultPeriod
  const showForecast =
    scope === 'month' &&
    !!insights &&
    insights.forecast.period !== '' &&
    insights.forecast.period === effectivePeriod

  const periodDisplay = useMemo(() => {
    if (scope === 'year') {
      return new Intl.DateTimeFormat(locale, { year: 'numeric' }).format(
        new Date(Number(effectivePeriod), 0, 1),
      )
    }
    const [y, m] = effectivePeriod.split('-').map(Number)
    return new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'long' }).format(
      new Date(y, m - 1, 1),
    )
  }, [scope, effectivePeriod, locale])

  const hasData = !!insights && insights.comparison.current.period !== ''

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-bold">{t('insights.page.title')}</h1>
          <p className="text-xs text-muted-foreground">{t('insights.page.subtitle')}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* 月/年 scope 切换 */}
          <div className="flex items-center rounded-lg border border-border/60 p-0.5">
            {(['month', 'year'] as InsightsScope[]).map((value) => (
              <Button
                key={value}
                variant="ghost"
                size="sm"
                className={`h-7 rounded-md px-2.5 text-xs ${
                  scope === value ? 'bg-primary/10 font-semibold text-primary' : 'text-muted-foreground'
                }`}
                onClick={() => switchScope(value)}
              >
                {t(`insights.scope.${value}`)}
              </Button>
            ))}
          </div>
          {/* 期次步进:‹ 期次 ›;当前期禁用 ›(不许看未来) */}
          <div className="flex items-center gap-1">
            <Button
              variant="outline"
              size="sm"
              className="h-8 w-8 p-0"
              aria-label={t('insights.period.prev')}
              onClick={() => stepPeriod(-1)}
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <YearMonthPicker
              year={Number(effectivePeriod.slice(0, 4))}
              month={scope === 'month' ? Number(effectivePeriod.slice(5, 7)) : null}
              onChange={(year, month) => {
                if (year === null) {
                  setCustomPeriod(null)
                } else if (month === null) {
                  setScope('year')
                  setCustomPeriod(String(year))
                } else {
                  setScope('month')
                  setCustomPeriod(`${year}-${String(month).padStart(2, '0')}`)
                }
              }}
            />
            <Button
              variant="outline"
              size="sm"
              className="h-8 w-8 p-0"
              aria-label={t('insights.period.next')}
              disabled={isCurrentPeriod}
              onClick={() => stepPeriod(1)}
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>

      <p className="font-mono text-[11px] text-muted-foreground/70">
        {periodDisplay}
        {isCurrentPeriod ? ` · ${t('insights.period.current')}` : ''}
      </p>

      {insights === null ? (
        <div className="flex h-40 items-center justify-center">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-muted border-t-primary" />
        </div>
      ) : hasData ? (
        <>
          <ComparisonCards comparison={insights.comparison} currency={currency} />
          <div className={`grid gap-4 ${showForecast ? 'lg:grid-cols-[1.5fr_1fr]' : ''}`}>
            <CategoryTrendChart
              trends={insights.category_trends}
              metric={metric}
              onMetricChange={setMetric}
            />
            {showForecast ? (
              <ForecastCard forecast={insights.forecast} currency={currency} />
            ) : null}
          </div>
        </>
      ) : (
        <div className="card flex flex-col items-center justify-center gap-1.5 rounded-2xl border border-dashed py-16 text-center">
          <p className="text-sm font-medium text-muted-foreground">{t('insights.empty.title')}</p>
          <p className="max-w-sm text-xs text-muted-foreground/80">{t('insights.empty.desc')}</p>
        </div>
      )}
    </div>
  )
}
