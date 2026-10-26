/**
 * Agent 回复的轻量 markdown 渲染(纯解析 + 薄渲染层)。
 *
 * 仓库没有任何 markdown 依赖(react-markdown 等),AskDialog 用纯文本渲染;
 * Agent 的财务分析回复结构性更强(标题/列表/加粗),又不想为它引入整棵
 * remark 依赖树 —— 这里手写一个受控子集解析器:
 *   - `### 标题`(1-4 个 #)
 *   - `- ` / `* ` / `• ` 无序列表
 *   - `1. ` 有序列表
 *   - `**加粗**`(行内,不支持嵌套)
 * 其它一切按普通段落处理(连续非空行合并为一段)。解析器是纯函数,
 * 渲染层(AgentRichText)只做结构映射,方便 vitest 直测 parse。
 */

export type AgentInline = { text: string; bold: boolean }

export type AgentBlock =
  | { kind: 'h'; text: string }
  | { kind: 'p'; lines: AgentInline[][] } // 每行一组 inline
  | { kind: 'quote'; text: string }
  | { kind: 'ul' | 'ol'; items: AgentInline[][] }

const UL_RE = /^[-*•]\s+/
const OL_RE = /^\d+[.、]\s*/
const H_RE = /^#{1,4}\s+/
const QUOTE_RE = /^>\s*/

/** 把一行文本按 `**bold**` 切成 inline 片段。 */
export function parseInline(line: string): AgentInline[] {
  const out: AgentInline[] = []
  let rest = line
  while (rest) {
    const idx = rest.indexOf('**')
    if (idx === -1) {
      out.push({ text: rest, bold: false })
      break
    }
    const end = rest.indexOf('**', idx + 2)
    if (end === -1) {
      out.push({ text: rest, bold: false })
      break
    }
    if (idx > 0) out.push({ text: rest.slice(0, idx), bold: false })
    const boldText = rest.slice(idx + 2, end)
    if (boldText) out.push({ text: boldText, bold: true })
    rest = rest.slice(end + 2)
  }
  return out.filter((p) => p.text.length > 0)
}

export function parseAgentMarkdown(text: string): AgentBlock[] {
  const blocks: AgentBlock[] = []
  const lines = (text || '').replace(/\r\n/g, '\n').split('\n')

  let para: AgentInline[][] = []
  let list: { kind: 'ul' | 'ol'; items: AgentInline[][] } | null = null

  const flushPara = () => {
    if (para.length) {
      blocks.push({ kind: 'p', lines: para })
      para = []
    }
  }
  const flushList = () => {
    if (list) {
      blocks.push(list)
      list = null
    }
  }

  for (const rawLine of lines) {
    const line = rawLine.trimEnd()
    if (!line.trim()) {
      flushPara()
      flushList()
      continue
    }
    if (H_RE.test(line)) {
      flushPara()
      flushList()
      blocks.push({ kind: 'h', text: line.replace(H_RE, '').trim() })
      continue
    }
    if (QUOTE_RE.test(line)) {
      flushPara()
      flushList()
      const text = line.replace(QUOTE_RE, '')
      const prev = blocks[blocks.length - 1]
      if (prev && prev.kind === 'quote') {
        prev.text += (prev.text ? '\n' : '') + text
      } else {
        blocks.push({ kind: 'quote', text })
      }
      continue
    }
    if (UL_RE.test(line)) {
      flushPara()
      if (!list || list.kind !== 'ul') {
        flushList()
        list = { kind: 'ul', items: [] }
      }
      list.items.push(parseInline(line.replace(UL_RE, '')))
      continue
    }
    if (OL_RE.test(line)) {
      flushPara()
      if (!list || list.kind !== 'ol') {
        flushList()
        list = { kind: 'ol', items: [] }
      }
      list.items.push(parseInline(line.replace(OL_RE, '')))
      continue
    }
    flushList()
    para.push(parseInline(line))
  }
  flushPara()
  flushList()
  return blocks
}
