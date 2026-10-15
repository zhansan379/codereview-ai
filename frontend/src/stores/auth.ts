import { defineStore } from 'pinia'
import { login as apiLogin, me as apiMe, getMenus, type User, type MenuGroup } from '../api'

const TOKEN_KEY = 'cr_token'
const PERMS_KEY = 'cr_perms'
const MENU_NAMES_KEY = 'cr_menu'
const MENU_GROUPS_KEY = 'cr_menus'

interface AuthState {
  token: string
  user: User | null
  permissions: string[]
  // 后端按权限过滤后的侧栏菜单树（供 LayoutView 渲染）
  menus: MenuGroup[]
  // 可访问路由 name 合集（供路由守卫按 name 兜底鉴权）
  menuNames: string[]
}

// 认证状态：JWT + 权限集 + 菜单存 sessionStorage（非 localStorage）
export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({
    token: sessionStorage.getItem(TOKEN_KEY) || '',
    user: null,
    permissions: JSON.parse(sessionStorage.getItem(PERMS_KEY) || '[]'),
    menus: JSON.parse(sessionStorage.getItem(MENU_GROUPS_KEY) || '[]'),
    menuNames: JSON.parse(sessionStorage.getItem(MENU_NAMES_KEY) || '[]'),
  }),
  getters: {
    isAuthed: (state) => !!state.token,
    role: (state) => state.user?.role_name || '',
    // 超管由后端在 permissions 里返回全量目录 → hasPerm 恒真
    hasPerm: (state) => (code: string) => state.permissions.includes(code),
  },
  actions: {
    // 拉取并按权限过滤的菜单，落 store + sessionStorage
    async loadMenus() {
      const data = await getMenus()
      this.menus = data.groups
      this.menuNames = data.names
      sessionStorage.setItem(MENU_GROUPS_KEY, JSON.stringify(data.groups))
      sessionStorage.setItem(MENU_NAMES_KEY, JSON.stringify(data.names))
      return data.names
    },
    async login(username: string, password: string) {
      const res = await apiLogin(username, password)
      this.token = res.access_token
      this.user = res.user
      this.permissions = res.permissions
      sessionStorage.setItem(TOKEN_KEY, res.access_token)
      sessionStorage.setItem(PERMS_KEY, JSON.stringify(res.permissions))
      // 登录后立即拉菜单：供侧栏渲染 + 守卫筛选路由
      await this.loadMenus()
      return res
    },
    // 硬刷新后重同步用户、权限与菜单
    async refresh() {
      if (!this.token) return null
      const res = await apiMe()
      this.user = res.user
      this.permissions = res.permissions
      sessionStorage.setItem(PERMS_KEY, JSON.stringify(res.permissions))
      await this.loadMenus()
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
      this.menus = []
      this.menuNames = []
      sessionStorage.removeItem(TOKEN_KEY)
      sessionStorage.removeItem(PERMS_KEY)
      sessionStorage.removeItem(MENU_NAMES_KEY)
      sessionStorage.removeItem(MENU_GROUPS_KEY)
    },
  },
})
