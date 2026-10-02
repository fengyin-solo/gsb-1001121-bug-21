import { defineStore } from 'pinia'

export type CurrentShift = {
  id: number
  crew: string
  version: number
  foreman: string
  foreman_code?: string
  driver: string
  driver_code?: string
  vehicle_plate: string
  started_at: string
  end_at: string | null
  is_current: boolean
  source?: string
  split_from_id?: number | null
} | null

type SessionState = {
  token: string
  userCode: string
  name: string
  crew: string
  role: string
  currentShift: CurrentShift
}

const TOKEN_KEY = 'patrol.session.token'
const USER_KEY = 'patrol.session.user'

function readUser(): Omit<SessionState, 'token' | 'currentShift'> {
  const raw = localStorage.getItem(USER_KEY)
  if (raw) {
    try {
      return JSON.parse(raw) as Omit<SessionState, 'token' | 'currentShift'>
    } catch {
      /* 落库内容损坏时按未登录处理 */
    }
  }
  return { userCode: '', name: '', crew: '', role: '' }
}

export const useSessionStore = defineStore('session', {
  state: (): SessionState => ({
    token: localStorage.getItem(TOKEN_KEY) ?? '',
    ...readUser(),
    currentShift: null,
  }),
  getters: {
    isLoggedIn: (state) => state.token.length > 0,
    canOperate(): boolean {
      // 派单/交接仅当前班次班长或值班员可用；旧班次身份会被后端再拦一次。
      return this.isLoggedIn && (this.role === '班长' || this.role === '值班员')
    },
    shiftLabel(state): string {
      const shift = state.currentShift
      if (!shift) {
        return state.crew ? `${state.crew}（未获取班次）` : '未登录'
      }
      return `${shift.crew} v${shift.version} · ${shift.foreman}/${shift.driver}`
    },
  },
  actions: {
    setSession(payload: {
      token: string
      user: { 工号: string; 姓名: string; 班组: string; 角色: string }
      shift: CurrentShift
    }) {
      this.token = payload.token
      this.userCode = payload.user.工号
      this.name = payload.user.姓名
      this.crew = payload.user.班组
      this.role = payload.user.角色
      this.currentShift = payload.shift
      localStorage.setItem(TOKEN_KEY, this.token)
      localStorage.setItem(
        USER_KEY,
        JSON.stringify({ userCode: this.userCode, name: this.name, crew: this.crew, role: this.role }),
      )
    },
    setCurrentShift(shift: CurrentShift) {
      this.currentShift = shift
    },
    clear() {
      this.token = ''
      this.userCode = ''
      this.name = ''
      this.crew = ''
      this.role = ''
      this.currentShift = null
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USER_KEY)
    },
  },
})
