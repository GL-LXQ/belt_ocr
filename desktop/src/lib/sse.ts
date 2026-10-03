export interface StreamEvent {
  event: string
  id: string
  data: string
}
/** 按 SSE 空行边界解析数据，保留跨网络分片的半行。Args: 分发回调。Returns: push 与 finish 方法。 */
export function createSseParser(dispatch: (event: StreamEvent) => void) {
  let pending = ''
  function push(chunk: string) {
    pending += chunk
    let boundary = pending.search(/\r?\n\r?\n/)
    while (boundary >= 0) {
      const separator = pending.slice(boundary).match(/^\r?\n\r?\n/)![0]
      const block = pending.slice(0, boundary)
      pending = pending.slice(boundary + separator.length)
      const event: StreamEvent = { event: 'message', id: '', data: '' }
      const lines: string[] = []
      block.split(/\r?\n/).forEach((line) => {
        if (line.startsWith(':')) return
        const colon = line.indexOf(':')
        const field = colon < 0 ? line : line.slice(0, colon)
        const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '')
        if (field === 'data') lines.push(value)
        else if (field === 'event') event.event = value
        else if (field === 'id') event.id = value
      })
      event.data = lines.join('\n')
      if (lines.length) dispatch(event)
      boundary = pending.search(/\r?\n\r?\n/)
    }
  }
  return { push }
}
