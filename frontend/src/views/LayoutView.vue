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
          <span>仪表盘</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('reviews:view')" index="/reviews">
          <el-icon><Document /></el-icon>
          <span>审查记录</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('projects:view')" index="/projects">
          <el-icon><Folder /></el-icon>
          <span>项目</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('models:manage')" index="/models">
          <el-icon><Cpu /></el-icon>
          <span>模型</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('notifiers:manage')" index="/notifiers">
          <el-icon><Bell /></el-icon>
          <span>IM 通知</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('schedules:manage')" index="/schedules">
          <el-icon><Timer /></el-icon>
          <span>定时任务</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('caches:manage')" index="/clone-caches">
          <el-icon><Box /></el-icon>
          <span>拉取缓存</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('settings:manage')" index="/settings">
          <el-icon><Setting /></el-icon>
          <span>设置</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('users:manage')" index="/users">
          <el-icon><User /></el-icon>
          <span>用户</span>
        </el-menu-item>
        <el-menu-item v-if="auth.hasPerm('roles:manage')" index="/roles">
          <el-icon><Key /></el-icon>
          <span>角色</span>
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
            :aria-label="isDark ? '切换到亮色模式' : '切换到暗色模式'"
          />
          <el-dropdown>
            <span class="user-chip">
              <el-icon><UserFilled /></el-icon>
              {{ auth.user?.display_name || auth.user?.username || '用户' }}
              <span class="role-tag">{{ auth.role }}</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="onLogout">退出登录</el-dropdown-item>
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
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
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
  UserFilled,
  Moon,
  Sunny,
} from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'
import { useDark } from '../composables/useDark'
import AppLogo from '../components/AppLogo.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { isDark } = useDark()

// 面包屑：从当前路由的 meta.title 出发，顺着 meta.parent 往上串出层级
// （路由表是平铺的，route.matched 只有 layout+叶子，串不出 审查记录 > 审查详情）。
const crumbs = computed(() => {
  const out: { name: string; title: string }[] = []
  const seen = new Set<string>()
  let name = route.name as string | undefined
  while (name && !seen.has(name)) {
    seen.add(name)
    const r = router.getRoutes().find((x) => x.name === name)
    const title = r?.meta?.title as string | undefined
    if (!title) break
    out.unshift({ name, title })
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