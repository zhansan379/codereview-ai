import { createI18n } from 'vue-i18n'

// i18n 入口。词条按「模块」拆在 modules/ 下,每个文件同时给出中英两份
// (并排放而不是分成 zh-CN.ts / en.ts 两棵树:漏译一眼就能看出来,
//  改文案也不用左右两个文件对着跳)。
//
// 命名空间 = 文件名:modules/dashboard.ts → t('dashboard.xxx')。
// 用 import.meta.glob 自动收集,新增模块不必回头改这里。

export const SUPPORTED_LOCALES = ['zh-CN', 'en'] as const
export type AppLocale = (typeof SUPPORTED_LOCALES)[number]

/** 默认中文:不读 navigator.language,免得现有用户在英文浏览器里突然看到英文界面 */
export const DEFAULT_LOCALE: AppLocale = 'zh-CN'

const STORAGE_KEY = 'cra-locale'

export function readSavedLocale(): AppLocale {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && (SUPPORTED_LOCALES as readonly string[]).includes(saved)) {
      return saved as AppLocale
    }
  } catch {
    // 隐私模式/禁用站点数据时读 localStorage 会抛,回退默认语言
  }
  return DEFAULT_LOCALE
}

export function saveLocale(locale: AppLocale): void {
  try {
    localStorage.setItem(STORAGE_KEY, locale)
  } catch {
    // 存不了就只在本次会话生效,不影响功能
  }
}

/** 每个模块导出 { 'zh-CN': {...}, en: {...} } */
type BilingualModule = Record<AppLocale, Record<string, unknown>>

const modules = import.meta.glob<{ default: BilingualModule }>('./modules/*.ts', { eager: true })

function buildMessages() {
  const out = { 'zh-CN': {}, en: {} } as Record<AppLocale, Record<string, unknown>>
  for (const [path, mod] of Object.entries(modules)) {
    const ns = path.replace(/^\.\/modules\//, '').replace(/\.ts$/, '')
    for (const locale of SUPPORTED_LOCALES) {
      out[locale][ns] = mod.default[locale]
    }
  }
  return out
}

const i18n = createI18n({
  // Composition API 模式。globalInjection 让模板里直接用 $t(),
  // 不必每个组件都写一遍 useI18n()——24 个文件的改动量差别很大。
  legacy: false,
  globalInjection: true,
  locale: readSavedLocale(),
  fallbackLocale: DEFAULT_LOCALE,
  messages: buildMessages(),
})

export default i18n

/** 供非组件模块(format.ts / usePoll.ts 等)取用。在 render/computed 里调用同样能跟踪语言切换。 */
export const { t } = i18n.global
