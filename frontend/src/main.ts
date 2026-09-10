import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
// 改用 SCSS 源码而非 dist/index.css：dist 是按默认变量预编译的，
// 只有走源码才能让 vite additionalData 注入的 variables.scss 生效。
import 'element-plus/theme-chalk/src/index.scss'
import '@/styles/element-plus-theme/index.scss'
import '@/styles/dark.scss'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'
import i18n from './locales'
// 副作用导入：mount 前就把 html.dark 落好，避免刷新时闪一下亮色
import './composables/useDark'
// 同理：mount 前定好 html[lang]，首屏语言与存的偏好一致
import './composables/useLocale'

// 入口：挂载 Pinia、Vue Router、i18n、Element Plus，注册图标组件（供模板中用 <el-icon><Xxx /></el-icon>）
const app = createApp(App)
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}
app.use(createPinia())
app.use(router)
app.use(i18n)
app.use(ElementPlus)
app.mount('#app')