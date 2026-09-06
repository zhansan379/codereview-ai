import axios from 'axios'
import router from '../router'

// 全局 axios 实例：baseURL 走 /api 前缀（开发环境由 vite 代理到后端）
const client = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// 请求拦截：附加 JWT（Bearer token），token 来自 sessionStorage 'cr_token'
client.interceptors.request.use((config) => {
  const token = sessionStorage.getItem('cr_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截：401 时清 token 并跳转登录页
client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response && error.response.status === 401) {
      sessionStorage.removeItem('cr_token')
      if (router.currentRoute.value.path !== '/login') {
        router.push('/login')
      }
    }
    return Promise.reject(error)
  }
)

export default client