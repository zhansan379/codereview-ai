// 审查记录 / 任务共用的展示辅助：本地时区时间格式化 + 状态标签样式。
// 后端存 UTC。SQLite 序列化可能不带时区后缀，这里统一补 +00:00 再转本地，避免
// 无时区标记的字符串默认按浏览器本地时区解析，导致显示比真实时间偏移 8 小时。

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

// 状态 → 中文标签
export function stateLabel(state: string): string {
  switch (state) {
    case 'queued': return '排队中'
    case 'running': return '运行中'
    case 'completed': return '审查成功'
    case 'success': return '成功'
    case 'reviewed': return '已审'
    case 'failed': return '失败'
    case 'skipped': return '已跳过'
    default: return state
  }
}