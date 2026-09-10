<template>
  <el-container class="layout">
    <el-aside width="220px" class="aside">
      <div class="logo">CodeReview AI</div>
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
        <div class="header-title"></div>
        <div class="header-right">
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
} from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

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
}
.aside {
  background: #fff;
  border-right: 1px solid #ebeef5;
}
.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: 700;
  color: #409eff;
}
.menu {
  border-right: none;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: #303133;
}
.role-tag {
  font-size: 12px;
  color: #909399;
  background: #f0f2f5;
  border-radius: 4px;
  padding: 1px 6px;
}
.main {
  background: #f0f2f5;
}
</style>