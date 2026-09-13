<template>
  <!-- el-tour 只认插槽里的 el-tour-step 子组件（2.14 的 steps prop 不参与渲染，
       传了会被当普通 attr 透传），步骤列表在 start*() 时构建 -->
  <el-tour v-model="open" :z-index="3000" @finish="markSeen" @close="markSeen">
    <el-tour-step v-for="(s, i) in steps" :key="i" v-bind="s" />
  </el-tour>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import type { TourStepProps } from 'element-plus'

// 漫游式引导分两种模式，避免每次打开都重复一遍全局介绍：
// - startFull()：全局骨架（欢迎/菜单/外观/语言/账户/回看）+ 当前页步骤，
//   只在首次登录自动弹一次；
// - start()：顶栏「?」触发，只讲当前页面的专属步骤——左侧菜单的每个页面
//   都注册了步骤，用户不用猜哪里有引导。
// 步骤目标用选择器字符串（data-tour 属性 / 布局类名），打开时按
// 「存在且可见」过滤：审查记录页两个 tab 的锚点是 v-show 双挂载，隐藏的
// 那份 querySelector 也找得到，必须当场剔除（tab 在蒙层下切不动，算一次即准）。

const { t } = useI18n()
const route = useRoute()

const open = ref(false)
const steps = ref<TourStepProps[]>([])

/** 元素存在且可见（v-show 隐藏的 display:none 没有 client rects） */
function visible(sel: string): boolean {
  const el = document.querySelector(sel)
  return !!el && el.getClientRects().length > 0
}

function step(target: string, placement: TourStepProps['placement'], titleKey: string, descKey: string, extra?: TourStepProps): TourStepProps {
  return { target, placement, title: t(titleKey), description: t(descKey), ...extra }
}

/** 各页面自己的步骤，按路由名注册；锚点不存在/不可见的步骤会被自动跳过 */
function pageSteps(): TourStepProps[] {
  const P: Record<string, TourStepProps[]> = {
    Dashboard: [
      step('[data-tour="kpi"]', 'bottom', 'tour.kpiTitle', 'tour.kpiDesc'),
      step('[data-tour="bands"]', 'bottom', 'tour.bandsTitle', 'tour.bandsDesc'),
      step('[data-tour="trend"]', 'bottom', 'tour.trendTitle', 'tour.trendDesc'),
      step('[data-tour="recent"]', 'top', 'tour.recentTitle', 'tour.recentDesc'),
    ],
    Reviews: [
      step('[data-tour="reviews-tabs"]', 'bottom', 'tour.reviewsTabsTitle', 'tour.reviewsTabsDesc'),
      step('[data-tour="reviews-mr-filter"]', 'bottom', 'tour.reviewsMrFilterTitle', 'tour.reviewsMrFilterDesc'),
      step('[data-tour="reviews-mr-list"]', 'top', 'tour.reviewsMrListTitle', 'tour.reviewsMrListDesc'),
      step('[data-tour="reviews-detail-filter"]', 'bottom', 'tour.reviewsDetailFilterTitle', 'tour.reviewsDetailFilterDesc'),
      step('[data-tour="reviews-detail-table"]', 'top', 'tour.reviewsDetailTableTitle', 'tour.reviewsDetailTableDesc'),
    ],
    Projects: [
      step('[data-tour="projects-toolbar"]', 'bottom', 'tour.projectsToolbarTitle', 'tour.projectsToolbarDesc'),
      step('[data-tour="projects-table"]', 'top', 'tour.projectsTableTitle', 'tour.projectsTableDesc'),
    ],
    Models: [
      step('[data-tour="models-toolbar"]', 'bottom', 'tour.modelsToolbarTitle', 'tour.modelsToolbarDesc'),
      step('[data-tour="models-table"]', 'top', 'tour.modelsTableTitle', 'tour.modelsTableDesc'),
    ],
    Notifiers: [
      step('[data-tour="notifiers-toolbar"]', 'bottom', 'tour.notifiersToolbarTitle', 'tour.notifiersToolbarDesc'),
      step('[data-tour="notifiers-table"]', 'top', 'tour.notifiersTableTitle', 'tour.notifiersTableDesc'),
    ],
    Schedules: [
      step('[data-tour="schedules-toolbar"]', 'bottom', 'tour.schedulesToolbarTitle', 'tour.schedulesToolbarDesc'),
      step('[data-tour="schedules-table"]', 'top', 'tour.schedulesTableTitle', 'tour.schedulesTableDesc'),
    ],
    CloneCaches: [
      step('[data-tour="caches-policy"]', 'bottom', 'tour.cachesPolicyTitle', 'tour.cachesPolicyDesc'),
      step('[data-tour="caches-list"]', 'top', 'tour.cachesListTitle', 'tour.cachesListDesc'),
    ],
    Settings: [
      step('[data-tour="settings-forge"]', 'bottom', 'tour.settingsForgeTitle', 'tour.settingsForgeDesc'),
      step('.concurrency-card', 'bottom', 'tour.settingsConcurrencyTitle', 'tour.settingsConcurrencyDesc'),
      step('.push-card', 'bottom', 'tour.settingsAutoTitle', 'tour.settingsAutoDesc'),
      step('.note-card', 'top', 'tour.settingsSecurityTitle', 'tour.settingsSecurityDesc'),
    ],
    Users: [
      step('[data-tour="users-toolbar"]', 'bottom', 'tour.usersToolbarTitle', 'tour.usersToolbarDesc'),
      step('[data-tour="users-table"]', 'top', 'tour.usersTableTitle', 'tour.usersTableDesc'),
    ],
    Roles: [
      step('[data-tour="roles-toolbar"]', 'bottom', 'tour.rolesToolbarTitle', 'tour.rolesToolbarDesc'),
      step('[data-tour="roles-table"]', 'top', 'tour.rolesTableTitle', 'tour.rolesTableDesc'),
    ],
  }
  return P[route.name as string] ?? []
}

/** 首次登录的全量引导：全局骨架 + 当前页步骤 */
function buildFullSteps(): TourStepProps[] {
  const all: TourStepProps[] = [
    step('.logo', 'right', 'tour.welcomeTitle', 'tour.welcomeDesc', {
      nextButtonProps: { children: t('tour.start') },
    }),
    step('.menu', 'right', 'tour.menuTitle', 'tour.menuDesc'),
    ...pageSteps(),
    // el-switch 会把 attrs 绑到内部隐藏 input 上，data-* 属性落不到根元素，改用类名锚定
    step('.dark-switch', 'bottom', 'tour.themeTitle', 'tour.themeDesc'),
    step('[data-tour="lang"]', 'bottom', 'tour.langTitle', 'tour.langDesc'),
    step('[data-tour="user"]', 'bottom', 'tour.userTitle', 'tour.userDesc'),
    step('[data-tour="help"]', 'bottom', 'tour.helpTitle', 'tour.helpDesc'),
  ]
  return all.filter((s) => typeof s.target !== 'string' || visible(s.target))
}

/** 顶栏「?」：只讲当前页面；没注册步骤的页面（如详情页）给一张居中说明卡 */
function buildPageSteps(): TourStepProps[] {
  const page = pageSteps().filter((s) => typeof s.target !== 'string' || visible(s.target))
  if (page.length) return page
  // 无 target 时 el-tour 居中显示；单步的末步按钮自动用 EP 语言包的「完成」
  return [
    {
      placement: 'bottom',
      title: t('tour.noTourTitle'),
      description: t('tour.noTourDesc'),
    },
  ]
}

function markSeen() {
  try {
    localStorage.setItem('cra-tour-done', '1')
  } catch {
    // 隐私模式存不了就算了：每次登录都会再弹一次，不影响功能
  }
}

function start() {
  steps.value = buildPageSteps()
  open.value = true
}

function startFull() {
  steps.value = buildFullSteps()
  open.value = true
}
defineExpose({ start, startFull })

onMounted(() => {
  let seen = false
  try {
    seen = localStorage.getItem('cra-tour-done') === '1'
  } catch {
    seen = false
  }
  if (seen) return
  // 缓一拍等首屏卡片渲染完成，避免引导先于页面出现
  setTimeout(() => {
    startFull()
  }, 600)
})
</script>
