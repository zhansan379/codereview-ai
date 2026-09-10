// 审查记录 / 任务共用的展示辅助：本地时区时间格式化 + 状态标签样式。
// 后端存 UTC。SQLite 序列化可能不带时区后缀，这里统一补 +00:00 再转本地，避免
// 无时区标记的字符串默认按浏览器本地时区解析，导致显示比真实时间偏移 8 小时。

import i18n, { t } from '../locales'

/** 枚举 → 词条；没有对应词条时回退显示原始值（后端新增枚举不至于显示成空白） */
export function enumLabel(kind: string, value: string | null | undefined): string {
  if (!value) return ''
  const key = `enum.${kind}.${value}`
  return i18n.global.te(key) ? t(key) : value
}

export function formatTime(v: string | null | undefined): string {
  if (!v) return '-'
  const iso = /[zZ]|[+-]\d{2}:?\d{2}$/.test(v) ? v : v + 'Z'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return v
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

// 状态 → Element Plus tag type（区分颜色，同色的状态不再糊在一起）
export function stateTagType(state: string): string {
  if (state === 'completed' || state === 'success' || state === 'reviewed') return 'success'
  if (state === 'failed') return 'danger'
  if (state === 'running') return 'warning' // 正在被 worker 审查（活跃）
  if (state === 'skipped' || state === 'queued') return 'info' // queued 只剩崩溃孤儿/重放前
  return 'warning'
}

// 状态 → 展示标签（词条见 locales/modules/enum.ts）
// 直接用 i18n.global 而不是 useI18n()：这几个函数被组件当普通工具函数调用，
// 保持签名不变就不用动所有调用点；在模板/computed 里调用时仍会跟踪 locale，切换即刷新。
export function stateLabel(state: string): string {
  return enumLabel('state', state)
}

// 执行模式 → 展示标签（NULL=未执行审查，如 skipped/failed/empty 空审）
export function modeLabel(mode: string | null | undefined): string {
  if (mode === 'agentic') return t('enum.mode.agentic')
  if (mode === 'diff') return t('enum.mode.diff')
  return t('enum.mode.notRun')
}

// 执行模式 → tag type（agent 高亮，diff 灰，未执行透明）
export function modeTagType(mode: string | null | undefined): string {
  if (mode === 'agentic') return 'primary'
  if (mode === 'diff') return 'info'
  return 'info'
}