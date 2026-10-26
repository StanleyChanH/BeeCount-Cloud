import { describe, expect, it } from 'vitest'

import { parseAgentMarkdown, parseInline } from './agentText'

describe('parseInline', () => {
  it('splits bold segments', () => {
    expect(parseInline('本月支出 **250 元**,环比 **+12%**')).toEqual([
      { text: '本月支出 ', bold: false },
      { text: '250 元', bold: true },
      { text: ',环比 ', bold: false },
      { text: '+12%', bold: true },
    ])
  })

  it('keeps plain text as one segment', () => {
    expect(parseInline('没有标记')).toEqual([{ text: '没有标记', bold: false }])
  })

  it('tolerates unmatched asterisks', () => {
    expect(parseInline('a ** b')).toEqual([{ text: 'a ** b', bold: false }])
    expect(parseInline('**bold')).toEqual([{ text: '**bold', bold: false }])
  })

  it('drops empty bold', () => {
    // 空 ** 对被吞掉,剩余文本按普通片段渲染(视觉等效 "ab")
    expect(parseInline('a****b')).toEqual([
      { text: 'a', bold: false },
      { text: 'b', bold: false },
    ])
  })
})

describe('parseAgentMarkdown', () => {
  it('parses heading + bullets + bold', () => {
    const blocks = parseAgentMarkdown('### 本月概览\n- 支出 **300** 元\n- 收入 1000 元\n\n储蓄率 70%。')
    expect(blocks).toEqual([
      { kind: 'h', text: '本月概览' },
      {
        kind: 'ul',
        items: [
          [
            { text: '支出 ', bold: false },
            { text: '300', bold: true },
            { text: ' 元', bold: false },
          ],
          [{ text: '收入 1000 元', bold: false }],
        ],
      },
      { kind: 'p', lines: [[{ text: '储蓄率 70%。', bold: false }]] },
    ])
  })

  it('groups consecutive plain lines into one paragraph', () => {
    const blocks = parseAgentMarkdown('第一行\n第二行\n\n新段落')
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toEqual({
      kind: 'p',
      lines: [
        [{ text: '第一行', bold: false }],
        [{ text: '第二行', bold: false }],
      ],
    })
    expect(blocks[1]).toEqual({ kind: 'p', lines: [[{ text: '新段落', bold: false }]] })
  })

  it('separates ordered from unordered lists', () => {
    const blocks = parseAgentMarkdown('1. 第一\n2. 第二\n- 混入无序')
    expect(blocks).toEqual([
      {
        kind: 'ol',
        items: [
          [{ text: '第一', bold: false }],
          [{ text: '第二', bold: false }],
        ],
      },
      { kind: 'ul', items: [[{ text: '混入无序', bold: false }]] },
    ])
  })

  it('treats 4-level heading and • bullets', () => {
    const blocks = parseAgentMarkdown('#### 建议\n• 少喝奶茶')
    expect(blocks[0]).toEqual({ kind: 'h', text: '建议' })
    expect(blocks[1]).toEqual({ kind: 'ul', items: [[{ text: '少喝奶茶', bold: false }]] })
  })

  it('merges consecutive quote lines into one blockquote', () => {
    const blocks = parseAgentMarkdown('> 第一行\n> 第二行\n\n正文')
    expect(blocks[0]).toEqual({ kind: 'quote', text: '第一行\n第二行' })
    expect(blocks[1]).toEqual({ kind: 'p', lines: [[{ text: '正文', bold: false }]] })
  })

  it('empty input yields no blocks', () => {
    expect(parseAgentMarkdown('')).toEqual([])
    expect(parseAgentMarkdown('\n\n')).toEqual([])
  })
})
