import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'

// 入口：挂载 Pinia、Vue Router、Element Plus，注册图标组件（供模板中用 <el-icon><Xxx /></el-icon>）
const app = createApp(App)
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}
app.use(createPinia())
app.use(router)
app.use(ElementPlus)
app.mount('#app')