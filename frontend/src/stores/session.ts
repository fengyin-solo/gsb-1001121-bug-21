import { defineStore } from 'pinia'

import { clearToken, fetchJson, getToken, onStaleIdentity, setToken } from '@/api/client'

type CrewOption = {
  id: number
  code: string
  name: string
  version: number
  current_leader: string | null
  current_shift: { id: number; label: string; signed_at: string } | null
}

type SessionSnapshot = {
  token: string
  leader: string
  crew_id: number
  crew_code: string
  crew_name: string
  version: number
  current_shift_id: number
  shift_label: string
  signed_at: string
}

export const useSessionStore = defineStore('session', {
  state: () => ({
    crews: [] as CrewOption[],
    session: null as SessionSnapshot | null,
    /** 后端 401：班组已交接、令牌过期；页面据此提示重新登录 */
    staleMessage: '',
  }),
  getters: {
    loggedIn: (state) => state.session !== null,
    canOperate: (state) => state.session !== null && state.session.leader.length > 0,
    currentCrew(state): CrewOption | null {
      if (!state.session) return null
      return state.crews.find((item) => item.id === state.session?.crew_id) ?? null
    },
    /** 派单/交接前的版本基线：提交时带回后端做乐观并发控制 */
    identityVersion(): number | null {
      return this.currentCrew?.version ?? this.session?.version ?? null
    },
  },
  actions: {
    async loadOptions() {
      const payload = await fetchJson<{ crews: CrewOption[] }>('/api/handover/options')
      this.crews = payload.crews
      return payload
    },
    async login(crewId: number, leader: string) {
      const snapshot = await fetchJson<SessionSnapshot>('/api/handover/auth/login', {
        method: 'POST',
        body: JSON.stringify({ crew_id: crewId, leader }),
      })
      setToken(snapshot.token)
      this.session = snapshot
      this.staleMessage = ''
      await this.loadOptions()
      return snapshot
    },
    logout() {
      clearToken()
      this.session = null
      this.staleMessage = ''
    },
    /** 刷新本地身份快照（交接后重新登录或拉 /auth/me 成功时调用） */
    async restore() {
      const token = getToken()
      if (!token) return
      try {
        this.session = await fetchJson<SessionSnapshot>('/api/handover/auth/me')
        await this.loadOptions()
        this.staleMessage = ''
      } catch {
        // 令牌失效时保持登出态，由 staleMessage 给出说明
        this.logout()
      }
    },
    bindStaleHandler() {
      onStaleIdentity((error) => {
        if (getToken()) {
          this.staleMessage = error.message
          clearToken()
          this.session = null
        }
      })
    },
  },
})
