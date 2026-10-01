/** 统一请求封装：拼后端地址、注入身份令牌、把 {code,message} 错误抛出。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export const TOKEN_KEY = 'handover-auth-token'

/** 后端约定的业务错误（与 HandoverError 对应）。 */
export class ApiError extends Error {
  code: string
  status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }

  get staleIdentity() {
    return this.status === 401
  }

  get conflict() {
    return this.status === 409
  }
}

type StaleListener = (error: ApiError) => void
const staleListeners = new Set<StaleListener>()

/** 会话被交接吊销时全局通知（页头清身份、页面提示重新登录）。 */
export function onStaleIdentity(listener: StaleListener): () => void {
  staleListeners.add(listener)
  return () => staleListeners.delete(listener)
}

export function getToken(): string {
  return window.localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string) {
  window.localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken() {
  window.localStorage.removeItem(TOKEN_KEY)
}

export async function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  const headers = new Headers(init?.headers)
  if (!headers.has('Content-Type') && init?.body) {
    headers.set('Content-Type', 'application/json')
  }
  const token = getToken()
  if (token) {
    headers.set('X-Auth-Token', token)
  }
  try {
    const response = await fetch(url, { ...init, headers })
    if (!response.ok) {
      let code = `HTTP_${response.status}`
      let message = `接口返回 ${response.status}，数据未更新`
      try {
        const payload = (await response.json()) as { code?: string; message?: string; detail?: string }
        code = payload.code ?? code
        message = payload.message ?? payload.detail ?? message
      } catch {
        // 非 JSON 错误体时沿用兜底文案
      }
      const error = new ApiError(code, message, response.status)
      if (error.staleIdentity) {
        staleListeners.forEach((listener) => listener(error))
      }
      throw error
    }
    return response
  } catch (error) {
    if (error instanceof ApiError) {
      throw error
    }
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  }
}

export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  return (await response.json()) as T
}
