import { defineStore } from 'pinia'
import { login as apiLogin, me as apiMe, type User, type Workspace } from '../api'

const TOKEN_KEY = 'cr_token'
const REFRESH_KEY = 'cr_refresh'
const PERMS_KEY = 'cr_perms'

interface AuthState {
  token: string
  user: User | null
  permissions: string[]
  // BYOK：当前用户作为 owner 的私有 workspace（/auth/me 返回；驱动「工作区设置」入口）
  workspace: Workspace | null
}

// 认证状态：JWT + 权限集存 sessionStorage（非 localStorage）
export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({
    token: sessionStorage.getItem(TOKEN_KEY) || '',
    user: null,
    permissions: JSON.parse(sessionStorage.getItem(PERMS_KEY) || '[]'),
    workspace: null,
  }),
  getters: {
    isAuthed: (state) => !!state.token,
    role: (state) => state.user?.role_name || '',
    // 超管由后端在 permissions 里返回全量目录 → hasPerm 恒真
    hasPerm: (state) => (code: string) => state.permissions.includes(code),
  },
  actions: {
    async login(username: string, password: string, captchaId = '', captchaAnswer = '') {
      const res = await apiLogin(username, password, captchaId, captchaAnswer)
      this.token = res.access_token
      this.user = res.user
      this.permissions = res.permissions
      sessionStorage.setItem(TOKEN_KEY, res.access_token)
      if (res.refresh_token) sessionStorage.setItem(REFRESH_KEY, res.refresh_token)
      sessionStorage.setItem(PERMS_KEY, JSON.stringify(res.permissions))
      return res
    },
    // 硬刷新后重同步用户与权限
    async refresh() {
      if (!this.token) return null
      const res = await apiMe()
      this.user = res.user
      this.permissions = res.permissions
      this.workspace = res.workspace ?? null
      sessionStorage.setItem(PERMS_KEY, JSON.stringify(res.permissions))
      return res
    },
    async ensurePermissions() {
      // 无缓存权限集时补齐（登录后或权限被他人改名时）
      if (!this.permissions.length) {
        try {
          await this.refresh()
        } catch { /* 401 由 client 拦截器跳登录 */ }
      }
    },
    logout() {
      this.token = ''
      this.user = null
      this.permissions = []
      sessionStorage.removeItem(TOKEN_KEY)
      sessionStorage.removeItem(REFRESH_KEY)
      sessionStorage.removeItem(PERMS_KEY)
    },
  },
})