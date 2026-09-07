// 补拉状态单例：模块作用域持有，跨组件/跨路由保持。
// 后台补拉任务本身不随前端页面生命周期存活（在 FastAPI 侧），这里只负责
// 「展示在途状态 + 轮询 /pulls/poll/status 直到结束并弹结果」。
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { pollPulls, pollPullStatus, type PollProgress } from '../api'

/** 是否有补拉正在后台跑（按钮 loading / 页内提示共用，切页回来仍为 true）。 */
export const pollBusy = ref(false)

/** 补拉进行中的逐条进度（模块级，切页保持；结束置 null）。 */
export const pollProgress = ref<PollProgress | null>(null)

let loop: ReturnType<typeof setInterval> | null = null
let resolved = false // 本轮结果是否已弹给用户（避免重复弹）

async function watchOnce(): Promise<void> {
  if (!pollBusy.value) return
  let s
  try {
    s = await pollPullStatus()
  } catch {
    return // 瞬时错误：下一拍再试
  }
  if (s.running) {
    if (s.progress) pollProgress.value = s.progress
    return
  }
  // 本轮结束
  pollBusy.value = false
  pollProgress.value = null
  if (loop) {
    clearInterval(loop)
    loop = null
  }
  if (resolved) return
  resolved = true
  if (s.error) {
    ElMessage.error(`补拉失败：${s.error}`)
  } else if (s.report) {
    const parts = [
      `扫描 ${s.report.prs} 个打开 PR/MR`,
      `新入队 ${s.report.new} 条审查`,
      `已审过跳过 ${s.report.skipped}`,
    ]
    if (s.report.errors.length) parts.push(`失败 ${s.report.errors.length}`)
    ElMessage.success(`补拉完成：${parts.join('，')}，审查在后台进行，可在审查记录页查看`)
    if (s.report.errors.length) console.warn('补拉失败明细', s.report.errors)
  }
}

function startWatch(): void {
  if (loop) clearInterval(loop)
  loop = setInterval(watchOnce, 2000)
  void watchOnce()
}

/** 触发一轮补拉并开始跟踪。若已在跑则直接进入跟踪（前端可任意切页）。 */
export async function triggerPoll(): Promise<void> {
  if (pollBusy.value) {
    startWatch() // 已在后台跑（可能来自别的页面/实例）→ 只续上状态跟踪
    return
  }
  try {
    await pollPulls() // 后台启动；在跑会 409
  } catch (e: any) {
    if (e?.response?.status === 409) {
      startWatch() // 正好另有一轮在跑 → 跟踪它
      return
    }
    ElMessage.error(e?.response?.data?.detail || '补拉触发失败')
    return
  }
  pollBusy.value = true
  pollProgress.value = null
  resolved = false
  startWatch()
}

/** 组件挂载时调用：若补拉正在后台跑（之前触发后切了页）→ 恢复在途显示并续跟踪。 */
export function resumePollWatchIfBusy(): void {
  if (pollBusy.value) startWatch()
}