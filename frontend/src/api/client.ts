import axios from 'axios'
import router from '../router'

const TOKEN_KEY = 'cr_token'
const REFRESH_KEY = 'cr_refresh'

// 全局 axios 实例：baseURL 走 /api 前缀（开发环境由 vite 代理到后端）
const client = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// 请求拦截：附加 JWT（Bearer token），token 来自 sessionStorage 'cr_token'
client.interceptors.request.use((config) => {
  const token = sessionStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// refresh 专用裸调用（不经响应拦截，避免自触发 401→刷新→401 死循环）
async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = sessionStorage.getItem(REFRESH_KEY)
  if (!refreshToken) return false
  try {
    const resp = await axios.post('/api/auth/refresh', { refresh_token: refreshToken })
    const data = resp.data
    sessionStorage.setItem(TOKEN_KEY, data.access_token)
    if (data.refresh_token) sessionStorage.setItem(REFRESH_KEY, data.refresh_token)
    return true
  } catch {
    return false
  }
}

let refreshing: Promise<boolean> | null = null

// 响应拦截：401 时先静默刷新一次并重放原请求；失败才清 token 跳登录。
// 登录/刷新端点自身返回 401 不重试（避免循环）。
client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const status = error?.response?.status
    const url: string = error?.config?.url || ''
    const alreadyRetried = !!error?.config?._retried
    if (status !== 401 || url.endsWith('/auth/login') || url.endsWith('/auth/refresh') || alreadyRetried) {
      return Promise.reject(error)
    }
    try {
      refreshing = refreshing || refreshAccessToken()
      const ok = await refreshing
      refreshing = null
      if (!ok) throw error
      const config = error.config
      config._retried = true
      delete config.headers?.Authorization // 让请求拦截重挂新 token
      return client(config)
    } catch (err) {
      refreshing = null
      sessionStorage.removeItem(TOKEN_KEY)
      sessionStorage.removeItem(REFRESH_KEY)
      if (router.currentRoute.value.path !== '/login') {
        router.push('/login')
      }
      return Promise.reject(err)
    }
  }
)

export default client