import { createRouter, createWebHistory } from 'vue-router'
import LayoutView from '../views/LayoutView.vue'

// 除 /login 外所有页面都挂在 LayoutView 之下（含侧边菜单、顶栏）
const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('../views/LoginView.vue'),
    },
    {
      path: '/',
      component: LayoutView,
      redirect: '/dashboard',
      children: [
        {
          path: '/dashboard',
          name: 'Dashboard',
          component: () => import('../views/DashboardView.vue'),
        },
        {
          path: '/reviews',
          name: 'Reviews',
          component: () => import('../views/ReviewTabsView.vue'),
        },
        {
          // 旧聚合页地址兜底：并入审查记录页的「MR 汇总」tab
          path: '/reviews/prs',
          redirect: { path: '/reviews', query: { tab: 'mr' } },
        },
        {
          path: '/reviews/:id',
          name: 'ReviewDetail',
          component: () => import('../views/ReviewDetailView.vue'),
        },
        {
          path: '/reviews/:id/conversation',
          name: 'ReviewConversation',
          component: () => import('../views/ConversationView.vue'),
        },
        {
          path: '/projects',
          name: 'Projects',
          component: () => import('../views/ProjectsView.vue'),
        },
        {
          path: '/models',
          name: 'Models',
          component: () => import('../views/ModelsView.vue'),
        },
        {
          path: '/notifiers',
          name: 'Notifiers',
          component: () => import('../views/NotifiersView.vue'),
        },
        {
          path: '/schedules',
          name: 'Schedules',
          component: () => import('../views/SchedulesView.vue'),
        },
        {
          path: '/clone-caches',
          name: 'CloneCaches',
          component: () => import('../views/CloneCachesView.vue'),
        },
        {
          // 任务页已并入审查记录页，保留旧地址兜底跳转
          path: '/tasks',
          redirect: '/reviews',
        },
        {
          path: '/settings',
          name: 'Settings',
          component: () => import('../views/SettingsView.vue'),
        },
        {
          path: '/users',
          name: 'Users',
          component: () => import('../views/UsersView.vue'),
          meta: { permission: 'users:manage' },
        },
        {
          path: '/roles',
          name: 'Roles',
          component: () => import('../views/RolesView.vue'),
          meta: { permission: 'roles:manage' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

// 全局守卫：无 token 时除 /login 外重定向到 /login；有 token 但命中需权限路由且无该权限 → /dashboard
router.beforeEach((to) => {
  const token = sessionStorage.getItem('cr_token')
  if (!token && to.path !== '/login') {
    return { path: '/login' }
  }
  if (token && to.path === '/login') {
    return { path: '/dashboard' }
  }
  if (token && to.meta?.permission) {
    const perms: string[] = JSON.parse(sessionStorage.getItem('cr_perms') || '[]')
    if (!perms.includes(to.meta.permission as string)) {
      return { path: '/dashboard' }
    }
  }
  return true
})

export default router