import { createRouter, createWebHistory } from 'vue-router'
import LayoutView from '../views/LayoutView.vue'

// 除 /login 外所有页面都挂在 LayoutView 之下（含侧边菜单、顶栏）
const router = createRouter({
  history: createWebHistory(),
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
          component: () => import('../views/ReviewsView.vue'),
        },
        {
          path: '/reviews/:id',
          name: 'ReviewDetail',
          component: () => import('../views/ReviewDetailView.vue'),
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
          path: '/tasks',
          name: 'Tasks',
          component: () => import('../views/TasksView.vue'),
        },
        {
          path: '/settings',
          name: 'Settings',
          component: () => import('../views/SettingsView.vue'),
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

// 全局守卫：无 token 时除 /login 外重定向到 /login
router.beforeEach((to) => {
  const token = sessionStorage.getItem('cr_token')
  if (!token && to.path !== '/login') {
    return { path: '/login' }
  }
  if (token && to.path === '/login') {
    return { path: '/dashboard' }
  }
  return true
})

export default router