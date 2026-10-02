/** 统一请求封装：拼后端地址、带身份令牌、抛网络错误、统一处理 401。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const TOKEN_KEY = 'patrol.session.token'

export type ApiError = Error & { status?: number; detail?: string }

export async function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  const token = localStorage.getItem(TOKEN_KEY) ?? ''
  const headers = new Headers(init?.headers)
  if (!headers.has('Content-Type') && init?.body) {
    headers.set('Content-Type', 'application/json')
  }
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  try {
    const response = await fetch(url, { ...init, headers })
    if (response.status === 401) {
      // 交接后旧令牌被回收：清掉本地登录态，由页面提示重新登录。
      localStorage.removeItem(TOKEN_KEY)
      window.dispatchEvent(new CustomEvent('patrol:unauthorized'))
    }
    return response
  } catch (error: unknown) {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  }
}

/** 读 JSON；业务失败时把后端 detail/message 一并抛给页脚展示。 */
export async function requestJson<T>(path: string, init?: RequestInit): Promise<{ response: Response; data: T }> {
  const response = await request(path, init)
  const data = (await response.json().catch(() => ({}))) as T & { detail?: string; message?: string }
  return { response, data }
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}
