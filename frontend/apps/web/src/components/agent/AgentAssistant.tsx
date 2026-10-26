import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  Eraser,
  Loader2,
  RotateCw,
  Send,
  Settings2,
  Sparkles,
  Wrench,
  X,
} from 'lucide-react'

import {
  ApiError,
  streamAgentChat,
  type AgentChatMessage,
} from '@beecount/api-client'
import { Button, useLocale, useT } from '@beecount/ui'

import { useAuth } from '../../context/AuthContext'
import { VoiceInputButton } from '../cmdk-ai/VoiceInputButton'
import { parseAgentMarkdown } from '../../lib/agentText'

/** 服务端工具名 → i18n 键(工具执行状态 pill 的标签)。 */
const TOOL_KEYS = [
  'list_ledgers',
  'get_analytics',
  'get_insights',
  'get_budgets',
  'get_net_worth',
  'list_transactions',
] as const

type ToolName = (typeof TOOL_KEYS)[number]

type ChatMsg = {
  role: 'user' | 'assistant'
  content: string
  /** assistant:本轮使用的工具轨迹(本地化标签) */
  toolNames?: string[]
  error?: boolean
}

type Status = 'idle' | 'busy' | 'error'

const QUICK_KEYS = ['analyze', 'compare', 'budget', 'advice'] as const

/**
 * 悬浮 AI 财务助手 — FAB + 聊天窗(AppShell 全局挂载,首页即可用)。
 *
 * 跟 ⌘K「问 AI」(文档 RAG)不同:这里走 POST /ai/agent/chat 的工具循环,
 * LLM 可读用户自己的账本数据(analytics / insights / budgets / net-worth /
 * transactions),做查询、分析与建议。事件流:status(工具执行)→
 * chunk(最终答案一次性)→ tool_calls → done。
 *
 * 会话是内存态(切 section 不丢,刷新清空);没配 LLM 时给配置引导卡。
 */
export function AgentAssistant() {
  const t = useT()
  const { locale } = useLocale()
  const { token } = useAuth()
  const navigate = useNavigate()

  const [open, setOpen] = useState(false)
  const [visible, setVisible] = useState(false) // 动画:先渲染再平移
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [draft, setDraft] = useState('')
  const [status, setStatus] = useState<Status>('idle')
  /** 正在执行的工具(fresh 时高亮 spinner);完事后并入 doneTools */
  const [activeTool, setActiveTool] = useState<ToolName | null>(null)
  const [doneTools, setDoneTools] = useState<ToolName[]>([])
  const [noProvider, setNoProvider] = useState(false)
  const cancelRef = useRef(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const toolLabel = useCallback(
    (name: string) => t(`agent.tool.${name}`) === `agent.tool.${name}` ? name : t(`agent.tool.${name}`),
    [t],
  )

  // 打开/关闭动画(translate-y + opacity)
  useEffect(() => {
    if (open) {
      setVisible(true)
      setTimeout(() => inputRef.current?.focus(), 50)
    } else {
      setVisible(false)
      cancelRef.current = true
      setActiveTool(null)
      setDoneTools([])
    }
  }, [open])

  // 新消息/工具状态变化时滚到底
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, activeTool, doneTools])

  const send = useCallback(
    (raw: string) => {
      const text = raw.trim()
      if (!text || status === 'busy' || !token) return

      const history: AgentChatMessage[] = messages
        .filter((m) => !m.error)
        .map((m) => ({ role: m.role, content: m.content }))
      const nextMessages: ChatMsg[] = [...messages, { role: 'user', content: text }]

      cancelRef.current = false
      setStatus('busy')
      setActiveTool(null)
      setDoneTools([])
      setNoProvider(false)
      setMessages(nextMessages)
      setDraft('')

      void (async () => {
        let answer = ''
        let toolNames: string[] = []
        const finalize = (err?: string) => {
          setMessages((prev) => {
            if (answer) {
              return [...prev, { role: 'assistant', content: answer, toolNames }]
            }
            return [
              ...prev,
              {
                role: 'assistant' as const,
                content: err || 'error',
                error: true,
              },
            ]
          })
          setStatus(err && !answer ? 'error' : 'idle')
          setActiveTool(null)
        }

        try {
          for await (const ev of streamAgentChat(token, {
            messages: [...history, { role: 'user', content: text }],
            locale,
          })) {
            if (cancelRef.current) return
            if (ev.type === 'status') {
              if (ev.phase === 'start') {
                setActiveTool(ev.tool as ToolName)
              } else {
                setDoneTools((prev) =>
                  prev.includes(ev.tool as ToolName) ? prev : [...prev, ev.tool as ToolName],
                )
                setActiveTool(null)
              }
            } else if (ev.type === 'chunk') {
              answer = ev.text
            } else if (ev.type === 'tool_calls') {
              toolNames = ev.items.map((i) => i.name)
            } else if (ev.type === 'error') {
              finalize(`${ev.error_code}: ${ev.message}`)
              return
            } else if (ev.type === 'done') {
              finalize()
              return
            }
          }
          // 流自然结束(没有 done 事件)— 按已有内容收尾
          finalize(answer ? undefined : 'stream ended unexpectedly')
        } catch (err) {
          if (cancelRef.current) return
          if (err instanceof ApiError) {
            if (err.code === 'AI_NO_CHAT_PROVIDER') {
              setNoProvider(true)
              setStatus('idle')
              return
            }
            finalize(`${err.code || 'AI_ERROR'}: ${err.message}`)
            return
          }
          finalize(String(err))
        }
      })()
    },
    [messages, status, token, locale],
  )

  const retryLast = useCallback(() => {
    const lastUser = [...messages].reverse().find((m) => m.role === 'user')
    if (!lastUser) return
    // 回退到该提问之前的状态再重发
    const idx = messages.lastIndexOf(lastUser)
    setMessages(messages.slice(0, idx))
    setTimeout(() => send(lastUser.content), 0)
  }, [messages, send])

  const clearAll = useCallback(() => {
    cancelRef.current = true
    setMessages([])
    setStatus('idle')
    setActiveTool(null)
    setDoneTools([])
    setNoProvider(false)
  }, [])

  const voiceLang =
    locale === 'zh-CN' ? 'zh-CN' : locale === 'zh-TW' ? 'zh-TW' : 'en-US'

  const busy = status === 'busy'

  return (
    <>
      {/* FAB — 移动端避开底部导航(bottom-20),桌面右下角;打开时隐藏 */}
      {!open ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label={t('agent.fab')}
          title={t('agent.fab')}
          className="group fixed bottom-20 right-4 z-50 flex h-12 w-12 items-center justify-center rounded-full border border-primary/30 bg-[linear-gradient(135deg,hsl(var(--primary)),hsl(var(--primary)/0.8))] text-primary-foreground shadow-[0_10px_30px_-10px_hsl(var(--primary)/0.7)] transition-transform hover:scale-105 active:scale-95 md:bottom-6 md:right-6"
        >
          <Sparkles className="h-5 w-5 transition-transform group-hover:rotate-12" />
        </button>
      ) : null}

      {/* 聊天窗 */}
      {open ? (
        <div
          role="dialog"
          aria-label={t('agent.title')}
          className={`fixed bottom-20 right-4 z-50 flex h-[min(68vh,580px)] w-[min(calc(100vw-2rem),400px)] flex-col overflow-hidden rounded-2xl border border-border/60 bg-card shadow-2xl backdrop-blur transition-all duration-200 md:bottom-6 md:right-6 ${
            visible ? 'translate-y-0 opacity-100' : 'translate-y-4 opacity-0'
          }`}
        >
          {/* header */}
          <div className="flex items-center gap-2 border-b border-border/50 px-3.5 py-2.5">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/12 text-primary">
              <Sparkles className="h-4 w-4" />
            </span>
            <div className="min-w-0 flex-1 leading-tight">
              <p className="truncate text-[13px] font-semibold">{t('agent.title')}</p>
              <p className="truncate text-[10px] text-muted-foreground">{t('agent.subtitle')}</p>
            </div>
            {messages.length > 0 ? (
              <button
                type="button"
                onClick={clearAll}
                aria-label={t('agent.clear')}
                title={t('agent.clear')}
                className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                <Eraser className="h-3.5 w-3.5" />
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label={t('agent.close')}
              className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          {/* 消息区 */}
          <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-3.5 py-3">
            {messages.length === 0 && !noProvider ? (
              <div className="space-y-3 pt-4">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Sparkles className="h-3.5 w-3.5 text-primary" />
                  <span>{t('agent.intro')}</span>
                </div>
                <div className="grid grid-cols-1 gap-1.5">
                  {QUICK_KEYS.map((key) => (
                    <button
                      key={key}
                      type="button"
                      disabled={busy}
                      onClick={() => send(t(`agent.quick.${key}`))}
                      className="rounded-lg border border-border/50 bg-muted/30 px-3 py-2 text-left text-xs text-foreground/90 transition-colors hover:border-primary/40 hover:bg-primary/10 hover:text-primary disabled:opacity-50"
                    >
                      {t(`agent.quick.${key}`)}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            {noProvider ? <NoProviderCard t={t} onGoSettings={() => navigate('/app/settings/ai')} /> : null}

            {messages.map((m, i) =>
              m.role === 'user' ? (
                <div key={i} className="flex justify-end">
                  <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md border border-primary/25 bg-primary/15 px-3 py-2 text-[13px] leading-relaxed text-foreground">
                    {m.content}
                  </div>
                </div>
              ) : (
                <div key={i} className="flex flex-col gap-1">
                  {m.toolNames && m.toolNames.length > 0 ? (
                    <div className="flex flex-wrap items-center gap-1">
                      <Wrench className="h-3 w-3 text-muted-foreground/70" />
                      {m.toolNames.map((name) => (
                        <span
                          key={name}
                          className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
                        >
                          {toolLabel(name)}
                        </span>
                      ))}
                    </div>
                  ) : null}
                  <div
                    className={`max-w-[92%] rounded-2xl rounded-bl-md px-3 py-2 text-[13px] leading-relaxed ${
                      m.error
                        ? 'border border-destructive/40 bg-destructive/10 text-destructive'
                        : 'bg-muted/40 text-foreground'
                    }`}
                  >
                    {m.error ? m.content : <AgentRichText text={m.content} />}
                  </div>
                  {m.error && i === messages.length - 1 && !busy ? (
                    <button
                      type="button"
                      onClick={retryLast}
                      className="inline-flex w-fit items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-primary"
                    >
                      <RotateCw className="h-3 w-3" />
                      {t('agent.retry')}
                    </button>
                  ) : null}
                </div>
              ),
            )}

            {/* 工具执行状态(进行中) */}
            {busy ? (
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  <span>{t('agent.thinking')}</span>
                </div>
                {activeTool ? (
                  <span className="inline-flex w-fit items-center gap-1 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
                    <Loader2 className="h-2.5 w-2.5 animate-spin" />
                    {toolLabel(activeTool)}
                  </span>
                ) : null}
                {doneTools.map((name) => (
                  <span
                    key={name}
                    className="inline-flex w-fit items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground"
                  >
                    ✓ {toolLabel(name)}
                  </span>
                ))}
              </div>
            ) : null}
          </div>

          {/* 输入区 */}
          <div className="border-t border-border/50 px-3 py-2.5">
            <div className="flex items-end gap-1.5">
              <textarea
                ref={inputRef}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.nativeEvent.isComposing && !e.shiftKey) {
                    e.preventDefault()
                    send(draft)
                  }
                }}
                rows={1}
                placeholder={t('agent.placeholder')}
                aria-label={t('agent.placeholder')}
                className="max-h-28 min-h-[36px] flex-1 resize-none rounded-lg border border-border/60 bg-background px-2.5 py-2 text-[13px] leading-relaxed text-foreground outline-none placeholder:text-muted-foreground/70 focus:border-primary/50"
              />
              <VoiceInputButton
                lang={voiceLang}
                onInterim={(text) => setDraft(text)}
                onFinal={(text) => setDraft(text)}
              />
              <Button
                type="button"
                size="sm"
                className="h-9 w-9 shrink-0 p-0"
                disabled={!draft.trim() || busy}
                onClick={() => send(draft)}
                aria-label={t('agent.send')}
              >
                <Send className="h-4 w-4" />
              </Button>
            </div>
            <p className="mt-1.5 text-[10px] text-muted-foreground/60">{t('agent.disclaimer')}</p>
          </div>
        </div>
      ) : null}
    </>
  )
}

/** Agent 回复的受控 markdown 渲染(标题/列表/加粗),见 lib/agentText。 */
function AgentRichText({ text }: { text: string }) {
  const blocks = useMemo(() => parseAgentMarkdown(text), [text])
  return (
    <div className="space-y-2">
      {blocks.map((block, bi) => {
        if (block.kind === 'h') {
          return (
            <p key={bi} className="text-[12px] font-semibold text-foreground">
              {block.text}
            </p>
          )
        }
        if (block.kind === 'quote') {
          return (
            <p
              key={bi}
              className="whitespace-pre-line break-words border-l-2 border-border pl-2 text-[12px] text-muted-foreground"
            >
              {block.text}
            </p>
          )
        }
        if (block.kind === 'p') {
          return (
            <p key={bi} className="whitespace-pre-line break-words">
              {block.lines.map((inlines, li) => (
                <span key={li}>
                  {inlines.map((seg, si) =>
                    seg.bold ? (
                      <strong key={si} className="font-semibold">
                        {seg.text}
                      </strong>
                    ) : (
                      <span key={si}>{seg.text}</span>
                    ),
                  )}
                  {li < block.lines.length - 1 ? '\n' : null}
                </span>
              ))}
            </p>
          )
        }
        const ListTag = block.kind === 'ol' ? 'ol' : 'ul'
        return (
          <ListTag
            key={bi}
            className={`space-y-1 pl-4 ${block.kind === 'ol' ? 'list-decimal' : 'list-disc'} marker:text-muted-foreground`}
          >
            {block.items.map((inlines, ii) => (
              <li key={ii} className="break-words">
                {inlines.map((seg, si) =>
                  seg.bold ? (
                    <strong key={si} className="font-semibold">
                      {seg.text}
                    </strong>
                  ) : (
                    <span key={si}>{seg.text}</span>
                  ),
                )}
              </li>
            ))}
          </ListTag>
        )
      })}
    </div>
  )
}

function NoProviderCard({ t, onGoSettings }: { t: ReturnType<typeof useT>; onGoSettings: () => void }) {
  return (
    <div className="space-y-2 rounded-xl border border-dashed border-border/70 bg-muted/20 p-3">
      <div className="flex items-center gap-1.5 text-xs font-medium text-amber-600 dark:text-amber-400">
        <AlertTriangle className="h-3.5 w-3.5" />
        {t('agent.noProvider.title')}
      </div>
      <p className="text-[12px] leading-relaxed text-muted-foreground">{t('agent.noProvider.body')}</p>
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs" onClick={onGoSettings}>
        <Settings2 className="mr-1 h-3 w-3" />
        {t('agent.noProvider.action')}
      </Button>
    </div>
  )
}
