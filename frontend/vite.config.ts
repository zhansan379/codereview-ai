import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Vite 配置：启用 Vue 插件；开发服务器把 /api 代理到后端 (localhost:5001)
// base=/admin/：产物由后端 mount_admin 托管在 /admin 下，静态资源引用 /admin/assets/...，
// 否则默认根路径 /assets/... 后端没托管会 404（页面空白）。
export default defineConfig({
  base: '/admin/',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:5001',
        changeOrigin: true,
      },
    },
  },
})