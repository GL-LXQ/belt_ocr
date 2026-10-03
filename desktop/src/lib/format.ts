import type { Measurement } from './types'
/**
 * 将服务器时间转换为本地显示文字。
 * Args: value: ISO 时间、秒时间戳或空值。
 * Returns: "2026/10/03 12:00:00"，空值返回 "—"。
 */
export function formatDate(value: string | number | null | undefined) {
  if (!value) return '—'
  const date = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      }).format(date)
}
/**
 * 保留频率数值的显示精度。
 * Args: value: 后端频率或空值。
 * Returns: "50.125"，空值返回 "—"。
 */
export function formatFrequency(value: number | null | undefined) {
  return value == null
    ? '—'
    : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 3 }).format(value)
}
/**
 * 将已保存的复核字段映射为界面状态。
 * Args: record: 服务器测量记录。
 * Returns: "normal"、"pending" 或 "reviewed"。
 */
export function reviewStatus(record: Measurement) {
  return record.reviewed_at ? 'reviewed' : record.needs_review ? 'pending' : 'normal'
}
/**
 * 展示已保存的人工文字或正式 OCR 文字。
 * Args: record: 服务器测量记录。
 * Returns: ["ABC"]，按服务器原顺序显示。
 */
export function effectiveLines(record: Measurement) {
  return record.reviewed_lines ?? record.recognized_lines
}
export const reviewLabels: Record<string, string> = {
  normal: '正常',
  pending: '待复核',
  reviewed: '已复核',
}
export const stageLabels: Record<string, string> = {
  session_start: '本轮启动',
  image_capture: '图像采集',
  frequency_collection: '频率采集',
  character_recognition: '字符识别',
  evidence_storage: '证据入库',
}
