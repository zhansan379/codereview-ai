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
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElNotification } from 'element-plus'
import { Warning } from '@element-plus/icons-vue'
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
import { listNotifications, acknowledgeNotification, acknowledgeAllNotifications, NotificationItem } from '../api'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { isDark } = useDark()
const { locale, setLocale } = useLocale()
const { t } = useI18n()

// ===== 系统消息提醒（通用）=====
const notifications = ref<NotificationItem[]>([])
const notificationInstances = ref<Map<number, any>>(new Map())
let pollTimer: ReturnType<typeof setInterval> | null = null

async function fetchNotifications() {
  try {
    const res = await listNotifications()
    const newNotifications = res.items.filter(
      (n) => !notifications.value.some((nn) => nn.id === n.id)
    )
    notifications.value = res.items
    // 为新消息显示 Notification
    for (const notification of newNotifications) {
      showNotification(notification)
    }
  } catch {
    // 静默失败，不影响主流程
  }
}

function showNotification(notification: NotificationItem) {
  // 根据 level 映射到 Element Plus 的 type
  const typeMap: Record<string, 'info' | 'warning' | 'error' | 'success'> = {
    info: 'info',
    warning: 'warning',
    error: 'error',
  }
  const epType = typeMap[notification.level] || 'info'

  // 从 metadata 中提取额外信息
  const metadata = notification.metadata || {}
  let detailHtml = `<p>${notification.message.replace(/\n/g, '<br>')}</p>`
  if (metadata.wrong_url || metadata.correct_url) {
    detailHtml = `
      <div style="line-height: 1.6;">
        <p>${notification.message.replace(/\n/g, '<br>')}</p>
        ${metadata.wrong_url ? `<p style="color: #f56c6c;">错误 URL：${metadata.wrong_url}</p>` : ''}
        ${metadata.correct_url ? `<p style="color: #67c23a;">正确 URL：${metadata.correct_url}</p>` : ''}
      </div>
    `
  }

  const instance = ElNotification({
    title: notification.title,
    type: epType,
    duration: 0, // 不自动关闭
    dangerouslyUseHTMLString: true,
    message: detailHtml,
    showClose: true,
    onClose: () => {
      handleNotificationClose(notification.id)
    },
  })
  notificationInstances.value.set(notification.id, instance)
}

async function handleNotificationClose(notificationId: number) {
  try {
    await acknowledgeNotification(notificationId)
    notifications.value = notifications.value.filter((n) => n.id !== notificationId)
    notificationInstances.value.delete(notificationId)
  } catch {
    // 静默失败
  }
}

async function acknowledgeAll() {
  try {
    await acknowledgeAllNotifications()
    // 关闭所有 Notification
    notificationInstances.value.forEach((n) => n.close())
    notificationInstances.value.clear()
    notifications.value = []
  } catch {
    // 静默失败
  }
}

function startPolling() {
  // 每 30 秒轮询一次
  pollTimer = setInterval(fetchNotifications, 30000)
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
</style>