import { watch } from 'vue'
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import LayoutView from '../views/LayoutView.vue'
import i18n, { t } from '../locales'

// 侧栏菜单不再是前端硬编码：由后端 GET /api/menus 按权限过滤后返回，前端据此渲染。
// 每个业务路由的 name 与后端菜单项 name 一一对应；登录/刷新后按菜单 names 动态挂载，
// 守卫再按 name 兜底（不在菜单里 = 无权限）。
const MENU_NAMES_KEY = 'cr_menu'
export const LAYOUT_ROUTE_NAME = 'Layout'

// —— 恒挂载：登录页 + 布局壳（含无需权限的仪表盘与旧地址兜底跳转）——
const constantRoutes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/LoginView.vue'),
  },
  {
    path: '/',
    name: LAYOUT_ROUTE_NAME,
    component: LayoutView,
    redirect: '/dashboard',
    children: [
      {
        path: '/dashboard',
        name: 'Dashboard',
        component: () => import('../views/DashboardView.vue'),
        meta: { titleKey: 'menu.dashboard' },
      },
      {
        path: '/403',
        name: 'Forbidden',
        component: () => import('../views/ForbiddenView.vue'),
        meta: { titleKey: 'common.forbidden.title' },
      },
      // 旧聚合页地址兜底：并入审查记录页的「MR 汇总」tab
      { path: '/reviews/prs', redirect: { path: '/reviews', query: { tab: 'mr' } } },
      // 任务页已并入审查记录页，保留旧地址兜底
      { path: '/tasks', redirect: '/reviews' },
    ],
  },
  // 兜底用**组件**而非 redirect：让守卫能拿到原始路径并做「挂载后重试/无权限跳 403」，
  // 避免解析阶段就被重定向到 /dashboard 造成闪屏。
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('../views/ForbiddenView.vue'),
  },
]

// —— 需权限业务路由：name 与后端菜单一一对应，登录后按菜单动态挂载 ——
const asyncRoutes: RouteRecordRaw[] = [
  {
    path: '/workrate',
    name: 'Workrate',
    component: () => import('../views/WorkrateView.vue'),
    meta: { titleKey: 'menu.workrate' },
  },
  {
    path: '/members',
    name: 'Members',
    component: () => import('../views/MembersView.vue'),
    meta: { titleKey: 'menu.members' },
  },
  {
    path: '/reviews',
    name: 'Reviews',
    component: () => import('../views/ReviewTabsView.vue'),
    meta: { titleKey: 'menu.reviews' },
  },
  {
    path: '/reviews/:id',
    name: 'ReviewDetail',
    component: () => import('../views/ReviewDetailView.vue'),
    meta: { titleKey: 'menu.reviewDetail', parent: 'Reviews' },
  },
  {
    path: '/reviews/:id/conversation',
    name: 'ReviewConversation',
    component: () => import('../views/ConversationView.vue'),
    meta: { titleKey: 'menu.conversation', parent: 'ReviewDetail' },
  },
  {
    path: '/projects',
    name: 'Projects',
    component: () => import('../views/ProjectsView.vue'),
    meta: { titleKey: 'menu.projects' },
  },
  {
    path: '/models',
    name: 'Models',
    component: () => import('../views/ModelsView.vue'),
    meta: { titleKey: 'menu.models' },
  },
  {
    path: '/notifiers',
    name: 'Notifiers',
    component: () => import('../views/NotifiersView.vue'),
    meta: { titleKey: 'menu.notifiers' },
  },
  {
    path: '/schedules',
    name: 'Schedules',
    component: () => import('../views/SchedulesView.vue'),
    meta: { titleKey: 'menu.schedules' },
  },
  {
    path: '/clone-caches',
    name: 'Caches',
    component: () => import('../views/CloneCachesView.vue'),
    meta: { titleKey: 'menu.caches' },
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('../views/SettingsView.vue'),
    meta: { titleKey: 'menu.settings' },
  },
  {
    path: '/users',
    name: 'Users',
    component: () => import('../views/UsersView.vue'),
    meta: { titleKey: 'menu.users' },
  },
  {
    path: '/roles',
    name: 'Roles',
    component: () => import('../views/RolesView.vue'),
    meta: { titleKey: 'menu.roles' },
  },
]

const ASYNC_ROUTE_NAMES = new Set(asyncRoutes.map((r) => r.name as string))

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: constantRoutes,
})

// —— 动态路由挂载：按后端菜单返回的 names 筛选挂载，幂等（签名不变则跳过）——
let mountedSignature = ''
let mountedNames: string[] = []

function readMenuNames(): string[] | null {
  const raw = sessionStorage.getItem(MENU_NAMES_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

/** 按菜单 names 动态挂载/卸载业务路由；返回是否发生了变更（供守卫决定是否重试导航）。 */
export function ensureDynamicRoutes(names: string[]): boolean {
  const wanted = asyncRoutes
    .map((r) => r.name as string)
    .filter((n) => names.includes(n))
  const signature = [...wanted].sort().join('|')
  if (signature === mountedSignature) return false
  for (const name of mountedNames) {
    if (router.hasRoute(name)) router.removeRoute(name)
  }
  for (const route of asyncRoutes) {
    if (typeof route.name === 'string' && wanted.includes(route.name)) {
      router.addRoute(LAYOUT_ROUTE_NAME, route)
    }
  }
  mountedNames = wanted
  mountedSignature = signature
  return true
}

// 冷启动（硬刷新）时用 sessionStorage 里缓存的菜单名先行挂载，让深链能直接命中目标路由
const cachedNames = readMenuNames()
if (cachedNames) ensureDynamicRoutes(cachedNames)

// 全局守卫：无 token → 登录页；有 token 但菜单未缓存 → 拉取后重试；
// 业务路由挂载后再判权限，未授权/未匹配 → /403。
router.beforeEach(async (to) => {
  const token = sessionStorage.getItem('cr_token')
  if (!token) {
    return to.path === '/login' ? true : { path: '/login' }
  }
  if (to.path === '/login') {
    return { path: '/dashboard' }
  }

  let names = readMenuNames()
  if (!names) {
    try {
      // 动态 import 规避 router ↔ api/client 的循环依赖
      const { getMenus } = await import('../api')
      const data = await getMenus()
      names = data.names
      sessionStorage.setItem(MENU_NAMES_KEY, JSON.stringify(data.names))
      sessionStorage.setItem('cr_menus', JSON.stringify(data.groups))
    } catch {
      // 拉菜单失败（含 token 失效）：清 token 回登录页，避免与「有 token 跳仪表盘」互相弹跳
      sessionStorage.removeItem('cr_token')
      return { path: '/login' }
    }
  }

  // 动态路由刚挂载/变更：按原始 fullPath 重试，命中新挂载的目标路由（不经过任何中转页）
  if (ensureDynamicRoutes(names)) {
    return { path: to.fullPath, replace: true }
  }
  // 已挂载后仍落到兜底（未授权业务路由 / 不存在的路径）→ 403；受控路由名不在菜单同样 403
  if (to.name === 'NotFound'
      || (ASYNC_ROUTE_NAMES.has(to.name as string) && !names.includes(to.name as string))) {
    return { path: '/403' }
  }
  return true
})

// 标签页标题：跟着路由和语言走（meta.titleKey 是词条 key，不是字面量）
function applyTitle(): void {
  const key = router.currentRoute.value.meta?.titleKey as string | undefined
  document.title = key ? `${t(key)} · ${t('menu.appTitle')}` : t('menu.appTitle')
}
router.afterEach(applyTitle)
// 切语言时当前页标题也要跟着变（此时不发生路由跳转，afterEach 不会触发）
watch(() => i18n.global.locale.value, applyTitle)

export default router
