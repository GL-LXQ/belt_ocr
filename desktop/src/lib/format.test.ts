import { describe, expect, it } from 'vitest'
import { formatOcrResultText } from './format'

describe('实时 OCR 分类排版', () => {
  it('缺失类别保留固定顺序和占位符', () => {
    expect(formatOcrResultText([])).toBe('20  --\n8  --\n3  --\n2  --')
    expect(formatOcrResultText(['123'])).toBe('20  --\n8  --\n3  123\n2  --')
  })
  it('同类多条保留输入顺序，分类展示不修改后端数组', () => {
    const lines = ['12', '1234568A', '123', 'ABCDEFGHIJKLMNOPQRST', '1234567A']
    const original = [...lines]
    expect(formatOcrResultText(lines)).toBe(
      '20  ABCDEFGHIJKLMNOPQRST\n8  1234568A\n    1234567A\n3  123\n2  12',
    )
    expect(lines).toEqual(original)
  })
  it('与旧界面一致，只展示四种正式类别', () => {
    expect(formatOcrResultText(['OTHER', 'AB'])).toBe('20  --\n8  --\n3  --\n2  AB')
  })
})
