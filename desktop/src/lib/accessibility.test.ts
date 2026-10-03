import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
const stylesheet = readFileSync(resolve(process.cwd(), 'src/style.css'), 'utf8')

/** 按源码计算相对亮度，不读取截图或像素。Args: 六位颜色值。Returns: 0 到 1 的亮度。 */
function luminance(color: string) {
  const channels = [1, 3, 5].map((offset) => parseInt(color.slice(offset, offset + 2), 16) / 255)
  const linear = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  )
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
}
/** 读取指定规则的最终颜色声明。Args: 选择器与属性。Returns: 六位颜色值。 */
function readColor(selector: string, property: 'color' | 'background') {
  let result = ''
  for (const match of stylesheet.matchAll(/([^{}]+)\{([^{}]+)\}/g)) {
    if (
      !match[1]
        .trim()
        .split(',')
        .map((value) => value.trim())
        .includes(selector)
    )
      continue
    const value = match[2].match(
      new RegExp(`(?:^|;)\\s*${property}:\\s*(#[0-9a-f]{6}|white)\\s*;`),
    )?.[1]
    if (value) result = value === 'white' ? '#ffffff' : value
  }
  if (!result) throw new Error(`未找到颜色：${selector} ${property}`)
  return result
}
/** 计算文本颜色对比度。Args: 前景与背景。Returns: 1 到 21 的比值。 */
function contrast(foreground: string, background: string) {
  const [dark, light] = [luminance(foreground), luminance(background)].sort(
    (left, right) => left - right,
  )
  return (light + 0.05) / (dark + 0.05)
}

describe('源码无障碍检查', () => {
  it.each([
    '.badge.normal',
    '.badge.online',
    '.badge.pending',
    '.badge.reviewed',
    '.badge.fault',
    '.badge.offline',
    '.notice.warning',
    '.notice.success',
    '.button.primary',
    '.button.danger',
    '.nav-link.active',
  ])('%s 文本对比度至少 4.5:1', (selector) => {
    expect(
      contrast(readColor(selector, 'color'), readColor(selector, 'background')),
    ).toBeGreaterThanOrEqual(4.5)
  })
  it('辅助文字在工作区背景上保持足够对比度', () => {
    const muted = stylesheet.match(/--muted:\s*(#[0-9a-f]{6})/)![1]
    expect(contrast(muted, '#f5f6f8')).toBeGreaterThanOrEqual(4.5)
  })
  it('提供减少运动、可见焦点和移动触控目标规则', () => {
    expect(stylesheet).toContain('prefers-reduced-motion: reduce')
    expect(stylesheet).toContain(':focus-visible')
    expect(stylesheet).toContain('min-height: 44px')
    expect(stylesheet).toContain('min-width: 320px')
  })
})
