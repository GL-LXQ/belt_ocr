import type { RuntimeMachine, Snapshot } from './types'
/** 按后端建立顺序选择当前或最近周期。Args: 快照、机器。Returns: 匹配周期或 undefined。 */
export function currentSession(snapshot: Snapshot | null, machine: RuntimeMachine) {
  const sessions = snapshot?.sessions.filter((session) => session.machine_id === machine.id) ?? []
  return machine.active_session_id
    ? sessions.find((session) => session.session_id === machine.active_session_id)
    : sessions[sessions.length - 1]
}
/** 动画只读取实际活动周期，不使用历史结果驱动现场运动。Args: 快照、机器。Returns: {extended, capturing, frequencyListening} 三个动画开关。 */
export function animationState(snapshot: Snapshot | null, machine: RuntimeMachine) {
  const session = snapshot?.sessions.find(
    (item) => item.machine_id === machine.id && item.session_id === machine.active_session_id,
  )
  const active = Boolean(
    snapshot?.status === 'running' && machine.active_session_id && session && !session.cycle_closed,
  )
  return {
    extended: active,
    capturing: active && session?.stages.image_capture === 'running',
    frequencyListening: active && session?.stages.frequency_collection === 'running',
  }
}
