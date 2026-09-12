<template>
  <el-container class="layout">
    <el-aside width="220px" class="aside">
      <div class="logo">
        <AppLogo :size="24" class="logo-mark" />
        <span>CodeReview AI</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        class="menu"
      >
        <el-menu-item v-if="auth.hasPerm('stats:view')" index="/dashboard">
          <el-icon><DataBoard /></el-icon>
          <span>{{ $t('menu.dashboard') }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('reviews:view')" index="/reviews">
          <el-icon><Document /></el-icon>
          <span>{{ $t('menu.reviews') }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('projects:view')" index="/projects">
          <el-icon><Folder /></el-icon>
          <span>{{ $t('menu.projects') }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('models:manage')" index="/models">
          <el-icon><Cpu /></el-icon>
          <span>{{ $t('menu.models') }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('notifiers:manage')" index="/notifiers">
          <el-icon><Bell /></el-icon>
          <span>{{ $t('menu.notifiers') }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('schedules:manage')" index="/schedules">
          <el-icon><Timer /></el-icon>
          <span>{{ $t('menu.schedules') }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('caches:manage')" index="/clone-caches">
          <el-icon><Box /></el-icon>
          <span>{{ $t('menu.caches') }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('settings:manage')" index="/settings">
          <el-icon><Setting /></el-icon>
          <span>{{ $t('menu.settings') }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('users:manage')" index="/users">
          <el-icon><User /></el-icon>
          <span>{{ $t('menu.users') }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('roles:manage')" index="/roles">
          <el-icon><Key /></el-icon>
          <span>{{ $t('menu.roles') }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <el-breadcrumb class="crumbs" separator="/">
          <el-breadcrumb-item
            v-for="(c, i) in crumbs"
            :key="c.name"
            :to="i < crumbs.length - 1 ? { name: c.name, params: route.params } : undefined"
          >
            {{ c.title }}
          </el-breadcrumb-item>
        </el-breadcrumb>
        <div class="header-right">
          <el-switch
            v-model="isDark"
            class="dark-switch"
            :active-action-icon="Moon"
            :inactive-action-icon="Sunny"
            :aria-label="isDark ? $t('menu.toLight') : $t('menu.toDark')"
          />
          <!-- 语言切换：@element-plus/icons-vue 里没有地球/语言类图标，用文字标识 -->
          <el-dropdown trigger="click" @command="setLocale">
            <span class="lang-chip" :title="$t('menu.language')">
              {{ locale === 'en' ? 'EN' : '中' }}
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="zh-CN" :disabled="locale === 'zh-CN'">中文</el-dropdown-item>
                <el-dropdown-item command="en" :disabled="locale === 'en'">English</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-dropdown>
            <span class="user-chip">
              {{ auth.user?.display_name || auth.user?.username || $t('menu.user') }}
              <span class="role-tag">{{ auth.role }}</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="onLogout">{{ $t('menu.logout') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>

  <!-- Webhook 配置错误提醒对话框 -->
  <el-dialog
    v-model="showDialog"
    title="Webhook 配置错误"
    width="600px"
    :close-on-click-modal="false"
  >
    <div class="webhook-error-content">
      <p style="color: #e6a23c; margin-bottom: 16px;">
        ⚠️ 检测到 {{ webhookErrors.length }} 条 webhook 路径配置错误，请修正后重新测试。
      </p>
      <el-scrollbar max-height="300px">
        <div
          v-for="error in webhookErrors"
          :key="error.id"
          class="webhook-error-item"
        >
          <div class="error-header">
            <strong>{{ error.provider.toUpperCase() }}</strong>
            <span class="error-time">{{ new Date(error.created_at).toLocaleString() }}</span>
          </div>
          <div class="error-url">
            <div class="wrong-url">❌ 错误：{{ error.wrong_url }}</div>
            <div class="correct-url">✅ 正确：{{ error.correct_url }}</div>
          </div>
          <div class="error-actions">
            <el-button size="small" @click="onAcknowledgeError(error)">已确认</el-button>
          </div>
        </div>
      </el-scrollbar>
    </div>
    <template #footer>
      <el-button @click="showDialog = false">关闭</el-button>
      <el-button type="primary" @click="onAcknowledgeAll">全部确认</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElDialog, ElButton } from 'element-plus'
import {
  DataBoard,
  Document,
  Folder,
  Cpu,
  Bell,
  Setting,
  Timer,
  Box,
  User,
  Key,
  Moon,
  Sunny,
} from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'
import { useDark } from '../composables/useDark'
import { useLocale } from '../composables/useLocale'
import AppLogo from '../components/AppLogo.vue'
import { listWebhookErrors, acknowledgeWebhookError, acknowledgeAllWebhookErrors, WebhookErrorItem } from '../api'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { isDark } = useDark()
const { locale, setLocale } = useLocale()
const { t } = useI18n()

// ===== Webhook 配置错误提醒（持久化）=====
const webhookErrors = ref<WebhookErrorItem[]>([])
const showDialog = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

async function fetchWebhookErrors() {
  try {
    const res = await listWebhookErrors()
    webhookErrors.value = res.items
    if (res.items.length > 0) {
      showDialog.value = true
    }
  } catch {
    // 静默失败，不影响主流程
  }
}

async function onAcknowledgeError(error: WebhookErrorItem) {
  try {
    await acknowledgeWebhookError(error.id)
    webhookErrors.value = webhookErrors.value.filter((e) => e.id !== error.id)
    if (webhookErrors.value.length === 0) {
      showDialog.value = false
    }
  } catch {
    // 静默失败
  }
}

async function onAcknowledgeAll() {
  try {
    await acknowledgeAllWebhookErrors()
    webhookErrors.value = []
    showDialog.value = false
  } catch {
    // 静默失败
  }
}

function startPolling() {
  // 每 30 秒轮询一次
  pollTimer = setInterval(fetchWebhookErrors, 30000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// 面包屑：从当前路由的 meta.titleKey 出发，顺着 meta.parent 往上串出层级
// （路由表是平铺的，route.matched 只有 layout+叶子，串不出 审查记录 > 审查详情）。
// t() 在 computed 里调用，切语言会自动重算，不用额外接线。
const crumbs = computed(() => {
  const out: { name: string; title: string }[] = []
  const seen = new Set<string>()
  let name = route.name as string | undefined
  while (name && !seen.has(name)) {
    seen.add(name)
    const r = router.getRoutes().find((x) => x.name === name)
    const titleKey = r?.meta?.titleKey as string | undefined
    if (!titleKey) break
    out.unshift({ name, title: t(titleKey) })
    name = r?.meta?.parent as string | undefined
  }
  return out
})

// 高亮当前菜单：审查详情页归属到「审查记录」（含 /reviews/prs 旧聚合链接）。
const activeMenu = computed(() => {
  if (route.path.startsWith('/reviews')) return '/reviews'
  return route.path
})

onMounted(() => {
  // 硬刷新后重同步用户与权限
  auth.refresh().catch(() => {})
  // 启动 webhook 错误轮询
  fetchWebhookErrors()
  startPolling()
})

onUnmounted(() => {
  stopPolling()
})

function onLogout() {
  auth.logout()
  router.push('/login')
}
</script>

<style scoped>
.layout {
  height: 100vh;
  /* 顶栏高度：EP 默认 60px，按需求压掉 1/5 → 48px。
     侧栏 logo 块共用同一个值，两边的分割线才对得齐。 */
  --app-header-height: 48px;
}
.aside {
  background: var(--el-bg-color);
  border-right: 1px solid var(--el-border-color-lighter);
}
.logo {
  height: var(--app-header-height);
  display: flex;
  align-items: center;
  /* 居左，左内边距对齐 el-menu-item 的默认 20px，图标与菜单项左缘成一条线 */
  padding-left: 20px;
  gap: 8px;
  font-size: 17px;
  font-weight: 700;
  color: var(--el-color-primary);
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.logo-mark {
  flex: none;
}
.menu {
  border-right: none;
}
.header {
  --el-header-height: var(--app-header-height);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.crumbs {
  font-size: 14px;
  line-height: 1;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: var(--el-text-color-primary);
}
/* 语言标识：与暗色开关同高的小方块，宽度固定，中/EN 切换时顶栏不抖 */
.lang-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 28px;
  height: 22px;
  padding: 0 6px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular);
  background: var(--el-bg-color-page);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}
.lang-chip:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary);
}
.role-tag {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-bg-color-page);
  border-radius: 4px;
  padding: 1px 6px;
}
.main {
  background: var(--el-bg-color-page);
}

/* Webhook 配置错误提醒对话框样式 */
.webhook-error-content {
  padding: 0 4px;
}

.webhook-error-item {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 12px;
  margin-bottom: 12px;
}

.webhook-error-item:last-child {
  margin-bottom: 0;
}

.error-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.error-header strong {
  color: var(--el-color-warning);
  font-size: 14px;
}

.error-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.error-url {
  margin-bottom: 8px;
}

.wrong-url {
  color: var(--el-color-danger);
  font-size: 13px;
  margin-bottom: 4px;
  word-break: break-all;
}

.correct-url {
  color: var(--el-color-success);
  font-size: 13px;
  word-break: break-all;
}

.error-actions {
  display: flex;
  justify-content: flex-end;
}
</style>