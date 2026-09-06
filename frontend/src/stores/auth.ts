import { defineStore } from 'pinia'
import { login as apiLogin } from '../api'

const TOKEN_KEY = 'cr_token'

interface AuthState {
  token: string
  user: any
}

// 认证状态：JWT 存 sessionStorage（非 localStorage）
export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({
    token: sessionStorage.getItem(TOKEN_KEY) || '',
    user: null,
  }),
  getters: {
    isAuthed: (state) => !!state.token,
  },
  actions: {
    async login(password: string) {
      const res = await apiLogin(password)
      this.token = res.access_token
      this.user = res.user
      sessionStorage.setItem(TOKEN_KEY, res.access_token)
    },
    logout() {
      this.token = ''
      this.user = null
      sessionStorage.removeItem(TOKEN_KEY)
    },
  },
})