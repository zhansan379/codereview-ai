import { ref, watch } from 'vue'

// 暗色模式开关：切 html.dark（Element Plus 暗色变量的挂载点，见 styles/dark.scss），
// 并把选择写进 localStorage。模块级单例，多处 useDark() 共享同一份状态。

const STORAGE_KEY = 'cra-dark'

function systemPrefersDark(): boolean {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
}

function initialValue(): boolean {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved !== null) return saved === '1'
  } catch {
    // 隐私模式/禁用站点数据时读 localStorage 会抛，回退到系统偏好
  }
  return systemPrefersDark()
}

const isDark = ref(initialValue())

function applyToDom(dark: boolean): void {
  document.documentElement.classList.toggle('dark', dark)
}

// 模块导入即生效：main.ts 在 mount 前 import 本模块，避免先亮后暗的闪屏
applyToDom(isDark.value)

watch(isDark, (dark) => {
  applyToDom(dark)
  try {
    localStorage.setItem(STORAGE_KEY, dark ? '1' : '0')
  } catch {
    // 存不了就只在本次会话生效，不影响功能
  }
})

export function useDark() {
  return { isDark }
}
