import { getCurrentInstance, onDeactivated, onScopeDispose, ref, shallowRef } from 'vue'
import { errorText } from '../lib/api'
/** 取消过期读取，避免旧筛选和旧详情覆盖当前内容。Args: 无。Returns: data、状态和 run 方法。 */
export function useRequest<T>() {
  const data = shallowRef<T | null>(null)
  const loading = ref(false)
  const error = ref('')
  let sequence = 0
  let controller: AbortController | null = null
  async function run(read: (signal: AbortSignal) => Promise<T>, clear = false) {
    controller?.abort()
    controller = new AbortController()
    const current = ++sequence
    loading.value = true
    error.value = ''
    if (clear) data.value = null
    try {
      const result = await read(controller.signal)
      if (current === sequence) data.value = result
      return current === sequence ? result : undefined
    } catch (failure) {
      if (
        current === sequence &&
        !(failure instanceof DOMException && failure.name === 'AbortError')
      )
        error.value = errorText(failure)
    } finally {
      if (current === sequence) loading.value = false
    }
  }
  function cancel() {
    sequence++
    controller?.abort()
    loading.value = false
  }
  onScopeDispose(cancel)
  if (getCurrentInstance()) onDeactivated(cancel)
  return { data, loading, error, run, cancel }
}
