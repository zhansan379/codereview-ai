import { computed, watch } from 'vue'
import en from 'element-plus/es/locale/lang/en'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import i18n, { SUPPORTED_LOCALES, saveLocale, type AppLocale } from '../locales'

// 语言开关:与 useDark 同构(模块级单例 + localStorage + watch 落 DOM),
// 区别是状态本体就是 i18n 的 locale,不另存一份,免得两处不同步。
//
// Element Plus 自己的内置文案(分页「共 x 条」、日期选择器、空态)不走我们的词条,
// 得把对应的 EP locale 包喂给 el-config-provider——见 App.vue。
// 注:改造前项目根本没配 EP locale,默认是英文,所以中文界面下这些位置一直是英文,顺带修掉。

const EP_LOCALES = { 'zh-CN': zhCn, en }

/** i18n.global.locale 在 legacy:false 下是可写 computed,直接当共享状态用 */
const locale = i18n.global.locale as unknown as { value: AppLocale }

function applyToDom(l: AppLocale): void {
  document.documentElement.lang = l
}

// 模块导入即生效:main.ts 在 mount 前 import 本模块,html[lang] 与首屏语言一致
applyToDom(locale.value)

watch(
  () => locale.value,
  (l) => {
    applyToDom(l)
    saveLocale(l)
  },
)

export function useLocale() {
  const epLocale = computed(() => EP_LOCALES[locale.value] ?? zhCn)

  function setLocale(l: AppLocale): void {
    locale.value = l
  }

  return { locale, epLocale, setLocale, supported: SUPPORTED_LOCALES }
}
