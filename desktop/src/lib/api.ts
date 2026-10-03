import type { Envelope } from './types'
let baseUrl = ''
let token = ''
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public field?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}
/** 保存仅驻留内存的本地连接参数。Args: 地址与令牌。Returns: 无。 */
export function configureApi(base: string, bearer = '') {
  baseUrl = base.replace(/\/$/, '').replace(/\/api\/v1$/, '')
  token = bearer
}
/** 构造 API 地址和鉴权头。Args: API 路径。Returns: {url, headers} 请求参数。 */
export function requestOptions(path: string) {
  const relative = path.startsWith('/api/v1/') ? path : `/api/v1${path}`
  return {
    url: `${baseUrl}${relative}`,
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  }
}
/** 读取统一响应并保留服务端字段错误。Args: 路径与请求参数。Returns: 服务端 data 数据。 */
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const connection = requestOptions(path)
  const response = await fetch(connection.url, {
    ...options,
    headers: {
      ...connection.headers,
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    },
  })
  let result: Envelope<T> & { data: T & { field?: string } }
  try {
    result = await response.json()
  } catch {
    throw new ApiError(
      `服务响应异常（${response.status}），请检查后端是否已启动。`,
      response.status,
    )
  }
  if (!response.ok || !result.success)
    throw new ApiError(
      result.message || `请求失败（${response.status}）`,
      response.status,
      result.data?.field,
    )
  return result.data
}
/** 使用鉴权读取图片，调用方负责释放对象地址。Args: 路径、取消信号。Returns: 图片 Blob。 */
export async function imageBlob(path: string, signal: AbortSignal): Promise<Blob> {
  const connection = requestOptions(path)
  const response = await fetch(connection.url, { headers: connection.headers, signal })
  if (!response.ok) throw new ApiError(`图片读取失败（${response.status}）`, response.status)
  return response.blob()
}
/** 将非空筛选项编码到查询字符串。Args: 筛选字段。Returns: 以 ? 开头的查询字符串。 */
export function query(values: Record<string, string | number | undefined>) {
  const parameters = new URLSearchParams()
  Object.entries(values).forEach(([name, value]) => {
    if (value !== '' && value !== undefined) parameters.set(name, String(value))
  })
  return `?${parameters.toString()}`
}
/** 整理界面可显示的错误。Args: 错误对象。Returns: 中文错误文字。 */
export function errorText(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请重试。'
}
