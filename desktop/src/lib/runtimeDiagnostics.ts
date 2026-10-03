/**
 * 输出带 UTC 时间和测量身份的运行诊断日志。
 * Args:
 *   trace: 当前处理步骤的固定标识。
 *   details: 机器、周期、快照和动画状态字段。
 * Returns:
 *   undefined // 日志已输出到前端控制台
 */
export function writeRuntimeDiagnostic(
  trace: string,
  details: Record<string, string | number | boolean | null>,
): void {
  // 将时间、处理步骤和状态字段整理为一行日志。
  const fields = Object.entries(details)
    .map(([name, value]) => `${name}=${JSON.stringify(value)}`)
    .join(' ')
  const message = `${new Date().toISOString()} INFO frontend.runtime trace=${trace} ${fields}`

  // 将本次处理记录输出到前端开发者工具。
  console.info(message)
}
