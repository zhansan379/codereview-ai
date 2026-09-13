<template>
  <div class="rt-wrap">
    <!-- 列设置入口：勾选显隐 + 上移/下移调序，偏好按 storageKey 存本机 localStorage。
         toolbarTarget 指向父页面里的容器（如筛选卡按钮区）时 Teleport 过去贴着现有按钮排，
         避免在表格上方独占一行；没传/找不到容器则回落为表格上方右侧的独立工具行。 -->
    <Teleport :to="teleportReady ? toolbarTarget : 'body'" :disabled="!teleportReady">
      <div class="rt-toolbar" :class="{ 'rt-toolbar--inline': teleportReady }">
        <el-popover placement="bottom-end" :width="280" trigger="click">
          <template #reference>
            <!-- 不套 el-tooltip：其浮层会盖住按钮吞掉点击，功能说明放弹层内提示行 -->
            <el-button size="small" circle :icon="Setting" :aria-label="$t('reviewsTable.columnsSetting')" />
          </template>
        <div class="rt-colset">
          <div class="rt-colset-title">{{ $t('reviewsTable.columnsSetting') }}</div>
          <div class="rt-colset-hint">{{ $t('reviewsTable.columnsHint') }}</div>
          <div v-for="col in settingColumns" :key="col.key" class="rt-colset-row">
            <el-checkbox
              :model-value="!isHidden(col.key)"
              @change="(shown: any) => toggleColumn(col.key, !!shown)"
            >{{ colLabel(col.key) }}</el-checkbox>
            <span class="rt-colset-ops">
              <el-button link size="small" :disabled="isFirst(col.key)" :aria-label="$t('reviewsTable.moveUp')" @click="moveColumn(col.key, -1)">
                <el-icon><Top /></el-icon>
              </el-button>
              <el-button link size="small" :disabled="isLast(col.key)" :aria-label="$t('reviewsTable.moveDown')" @click="moveColumn(col.key, 1)">
                <el-icon><Bottom /></el-icon>
              </el-button>
            </span>
          </div>
          <div class="rt-colset-footer">
            <el-button link type="primary" size="small" @click="resetColumns">{{ $t('reviewsTable.resetColumns') }}</el-button>
          </div>
        </div>
        </el-popover>
      </div>
    </Teleport>

    <el-table :data="items" v-loading="loading" stripe @selection-change="onSelectionChange">
      <el-table-column v-if="selectable" type="selection" width="42" />
      <el-table-column
        v-for="col in visibleColumns"
        :key="col.key + '-' + renderVersion"
        v-bind="col.attrs"
        :label="colLabel(col.key)"
      >
        <template #default="{ row }">
          <template v-if="col.key === 'author'">{{ row.pr_author || '—' }}</template>
          <template v-else-if="col.key === 'mode'">
            <el-tag :type="modeTagType(row.exec_mode)" size="small">{{ modeLabel(row.exec_mode) }}</el-tag>
          </template>
          <template v-else-if="col.key === 'state'">
            <!-- 未开始行：标签只留状态词，原因收进小问号图标的 tooltip（浮层显示，不挤表格） -->
            <el-tag :type="stateTagType(row.state)">
              <span style="display: inline-flex; align-items: center; gap: 2px">
                {{ stateLabel(row.state) }}
                <el-tooltip
                  v-if="row.state === 'skipped' && row.skip_reason"
                  :content="skipReasonLabel(row.skip_reason)"
                  placement="top"
                >
                  <el-icon style="vertical-align: -2px"><QuestionFilled /></el-icon>
                </el-tooltip>
              </span>
            </el-tag>
          </template>
          <template v-else-if="col.key === 'time'">{{ formatTime(row[timeField]) }}</template>
          <template v-else>{{ row[col.rowProp!] }}</template>
        </template>
      </el-table-column>
      <!-- 宽度按最挤的一行给：详情 + 删除 + 补审/重试 + 重新发送 四个按钮同时出现时，
           中文需 ~200px（含按钮间距 12px×3 与单元格内边距 24px），英文需 ~238px。
           原先给的 160px 中文就会折行，把行高撑高——这里一并修掉。
           操作列固定最右，不参与列设置排序。 -->
      <el-table-column v-if="showAction || showRetry || showDelete || showStop" :label="$t('common.actions')" :width="colWidth(showDelete ? 205 : 120)"
        fixed="right">
        <template #default="{ row }">
          <el-button v-if="showAction" link type="primary" @click="$emit('detail', row.id)">{{ $t('common.detail') }}</el-button>
          <el-button v-if="showDelete" link type="danger" @click="$emit('delete', row.id)">{{ $t('common.delete') }}</el-button>
          <el-button v-if="showStop && row.state === 'queued'" link type="warning" :loading="stoppingId === row.id"
            @click="$emit('stop', row)">{{ $t('reviewsTable.stop') }}</el-button>
          <el-button v-if="showRetry && isRetryable(row)" link type="danger" :loading="retryingId === row.id"
            @click="$emit('retry', row)">{{ row.state === 'skipped' ? $t('reviewsTable.execute') : $t('reviewsTable.retry') }}</el-button>
          <el-button v-if="showRedeliver && row.writeback_failed" link type="warning" :loading="redeliveringId === row.id"
            @click="$emit('redeliver', row)">{{ $t('reviewsTable.resend') }}</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Bottom, QuestionFilled, Setting, Top } from '@element-plus/icons-vue'
import { formatTime, stateTagType, stateLabel, skipReasonLabel, modeLabel, modeTagType } from '../utils/format'
import type { ReviewItem } from '../api'
import { colWidth } from '../composables/useLocale'

// 审查记录 / 仪表盘"最近记录"共用的表格：列统一，改动一处多处生效。
// - timeField 决定显示"排队时间"还是"完成时间"（标签与列宽随之切换）。
// - showProcess 控制是否显示"事件类型/分支/尝试次数"过程列（审查记录页合并任务后开启）。
// - showRetry 控制"重试/执行"按钮（failed 行重试；skipped=未开始行执行），配合 retryingId 显示该行按钮 loading。
// - showStop 控制"停止"按钮（仅 queued 行；排队中 → 未开始），配合 stoppingId 显示 loading。
// - selectable 开启多选列（批量操作用），勾选变化经 selection-change 抛给父级。
// - storageKey 区分列设置偏好的存储位置（审查记录页 / 仪表盘各自记一份）。
const props = withDefaults(
  defineProps<{
    items: ReviewItem[]
    loading?: boolean
    /** 是否显示"详情"按钮 */
    showAction?: boolean
    /** 是否显示"事件类型/分支/尝试次数"过程列（审查记录页开启；仪表盘精简关） */
    showProcess?: boolean
    /** 是否显示"重试/执行"按钮（failed 行重试、未开始行执行；审查记录页开启；仪表盘关） */
    showRetry?: boolean
    /** 是否显示"停止"按钮（仅 queued 行） */
    showStop?: boolean
    /** 是否显示多选列（审查记录页批量操作开启；仪表盘关） */
    selectable?: boolean
    /** 是否显示"删除"按钮（审查记录页开启；仪表盘共用表格关，默认关） */
    showDelete?: boolean
    /** 是否显示"重新发送"按钮（仅 writeback_failed 行；审查记录页开启） */
    showRedeliver?: boolean
    /** 当前正在重试的行 id，用于锁住对应"重试"按钮的 loading 态 */
    retryingId?: number | null
    /** 当前正在停止的行 id，用于锁住对应"停止"按钮的 loading 态 */
    stoppingId?: number | null
    /** 当前正在重发的行 id，用于锁住对应"重新发送"按钮的 loading 态 */
    redeliveringId?: number | null
    /** 展示哪个时间字段：排队时间或完成时间 */
    timeField?: 'queued_at' | 'finished_at'
    /** 列设置偏好的 localStorage 键后缀，两个使用方各存一份 */
    storageKey?: string
    /** 列设置按钮的投送容器 CSS 选择器（父页面筛选卡按钮区等）；空/找不到则回落为表格上方独立工具行 */
    toolbarTarget?: string
  }>(),
  {
    loading: false,
    showAction: true,
    showProcess: false,
    showRetry: false,
    showStop: false,
    selectable: false,
    showRedeliver: false,
    showDelete: false,
    retryingId: null,
    stoppingId: null,
    redeliveringId: null,
    timeField: 'queued_at',
    storageKey: 'default',
    toolbarTarget: '',
  },
)

const emit = defineEmits<{
  (e: 'detail', id: number): void
  (e: 'retry', row: ReviewItem): void
  (e: 'stop', row: ReviewItem): void
  (e: 'redeliver', row: ReviewItem): void
  (e: 'delete', id: number): void
  (e: 'selection-change', rows: ReviewItem[]): void
}>()

const { t } = useI18n()

// ---------- 列设置按钮的落点 ----------
// Teleport 目标须在挂载时已存在：本组件在父模板里排在筛选卡之后，卡片 DOM 先行插入，onMounted 时必可查到；
// 查不到（没传/布局改动）就保持 disabled 走表格上方独立工具行，不报错不丢功能。
const teleportReady = ref(false)
onMounted(() => {
  teleportReady.value = !!props.toolbarTarget && !!document.querySelector(props.toolbarTarget)
})

// ---------- 列设置 ----------
// 列改成配置数组驱动：顺序即展示顺序，显隐走 hidden 集合，操作列/多选列不参与。
// rowProp：直接取行字段展示的列；没有 rowProp 的列（作者/模式/状态/时间）走模板里的专属分支。
type ColKey =
  | 'id' | 'pr_number' | 'pr_title' | 'author' | 'provider' | 'repo_id'
  | 'event' | 'branch' | 'attempt' | 'score' | 'mode' | 'state' | 'time'

interface ColumnDef {
  key: ColKey
  /** 透传给 el-table-column 的 prop/宽度等属性 */
  attrs: Record<string, unknown>
  /** 行字段名（纯文本列用） */
  rowProp?: keyof ReviewItem & string
  /** 仅 showProcess 开启时可选的过程列 */
  processOnly?: boolean
}

// computed 包一层：attrs 里的 colWidth() 读 locale，语言切换时宽度跟着重算
const allColumns = computed<ColumnDef[]>(() => [
  { key: 'id', attrs: { prop: 'id', width: 50 }, rowProp: 'id' },
  { key: 'pr_number', attrs: { prop: 'pr_number', width: 50 }, rowProp: 'pr_number' },
  { key: 'pr_title', attrs: { prop: 'pr_title', minWidth: 180, showOverflowTooltip: true }, rowProp: 'pr_title' },
  { key: 'author', attrs: { width: 120, showOverflowTooltip: true } },
  { key: 'provider', attrs: { prop: 'provider', width: colWidth(80) }, rowProp: 'provider' },
  { key: 'repo_id', attrs: { prop: 'repo_id', minWidth: 200, showOverflowTooltip: true }, rowProp: 'repo_id' },
  { key: 'event', attrs: { prop: 'event_type', width: colWidth(60) }, rowProp: 'event_type', processOnly: true },
  { key: 'branch', attrs: { prop: 'branch', width: 200, showOverflowTooltip: true }, rowProp: 'branch', processOnly: true },
  { key: 'attempt', attrs: { prop: 'attempt', width: colWidth(80) }, rowProp: 'attempt', processOnly: true },
  { key: 'score', attrs: { prop: 'score_total', width: colWidth(60) }, rowProp: 'score_total' },
  { key: 'mode', attrs: { width: 80 } },
  { key: 'state', attrs: { width: 100 } },
  { key: 'time', attrs: { width: 160 } },
])

// 当前用法下可选的列（仪表盘没有过程列，列设置里也不出现）
const settingColumns = computed(() => allColumns.value.filter((c) => !c.processOnly || props.showProcess))

// 列头文案：id/pr_number 是专有名词不走词条，时间列标签跟 timeField 走
const colLabel = (key: ColKey): string => {
  if (key === 'id') return 'ID'
  if (key === 'pr_number') return 'PR'
  if (key === 'time') return props.timeField === 'finished_at' ? t('reviewsTable.finishedAt') : t('reviewsTable.queuedAt')
  const i18nKeys: Partial<Record<ColKey, string>> = {
    pr_title: 'title', author: 'author', provider: 'provider', repo_id: 'repoId', event: 'event',
    branch: 'branch', attempt: 'attempt', score: 'score', mode: 'mode', state: 'state',
  }
  return t(`reviewsTable.${i18nKeys[key]}`)
}

const STORAGE_PREFIX = 'reviews-table-columns:'

// 读用户偏好：order 缺的列补到末尾、多余/不适用的列剔除，脏数据整体降级为默认
function loadPrefs(): { order: ColKey[]; hidden: ColKey[] } {
  const defaults = settingColumns.value.map((c) => c.key)
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + props.storageKey)
    if (!raw) return { order: [...defaults], hidden: [] }
    const parsed = JSON.parse(raw) as { order?: unknown; hidden?: unknown }
    const known = new Set<ColKey>(defaults)
    const order = (Array.isArray(parsed.order) ? parsed.order : []).filter(
      (k): k is ColKey => typeof k === 'string' && known.has(k as ColKey),
    )
    for (const k of defaults) if (!order.includes(k)) order.push(k)
    const hidden = (Array.isArray(parsed.hidden) ? parsed.hidden : []).filter(
      (k): k is ColKey => typeof k === 'string' && known.has(k as ColKey),
    )
    return { order, hidden }
  } catch {
    return { order: [...defaults], hidden: [] }
  }
}

const prefs = ref(loadPrefs())

function persist() {
  try {
    localStorage.setItem(STORAGE_PREFIX + props.storageKey, JSON.stringify({ order: prefs.value.order, hidden: prefs.value.hidden }))
  } catch {
    // 隐私模式等写不进 localStorage 就只用本次会话的选择
  }
}

const visibleColumns = computed(() => {
  const byKey = new Map(settingColumns.value.map((c) => [c.key, c]))
  return prefs.value.order
    .filter((k) => byKey.has(k) && !prefs.value.hidden.includes(k))
    .map((k) => byKey.get(k)!)
})

// EP 的列顺序在挂载时按 DOM 位置登记进表格 store，v-for 稳定 key 移动节点不会重新登记；
// 换序/重置时 bump 版本号强制列重挂载，新顺序才会生效（显隐走增删节点，不需要 bump）。
const renderVersion = ref(0)

const isHidden = (key: ColKey) => prefs.value.hidden.includes(key)

function toggleColumn(key: ColKey, shown: boolean) {
  const hidden = new Set(prefs.value.hidden)
  if (shown) hidden.delete(key)
  else hidden.add(key)
  prefs.value.hidden = [...hidden]
  persist()
}

function moveColumn(key: ColKey, dir: -1 | 1) {
  const order = [...prefs.value.order]
  const i = order.indexOf(key)
  const j = i + dir
  if (i < 0 || j < 0 || j >= order.length) return
  ;[order[i], order[j]] = [order[j], order[i]]
  prefs.value.order = order
  renderVersion.value++
  persist()
}

function resetColumns() {
  prefs.value = { order: settingColumns.value.map((c) => c.key), hidden: [] }
  renderVersion.value++
  persist()
}

const isFirst = (key: ColKey) => prefs.value.order[0] === key
const isLast = (key: ColKey) => prefs.value.order[prefs.value.order.length - 1] === key

function onSelectionChange(rows: ReviewItem[]) {
  emit('selection-change', rows)
}

// 该行是否可重试/执行，与后端 _retryable()（api/admin/tasks.py）保持一致：
// - failed → 重试（审过但出错，重跑一次）；
// - skipped（未开始：门控跳过/未配置 LLM/手动停止/分支已删）→ 执行
//   （置 force_rerun 绕过门控强制审一次；分支已删的执行会自然失败转 failed，能看到原因）。
function isRetryable(row: ReviewItem): boolean {
  return row.state === 'failed' || row.state === 'skipped'
}
</script>

<!-- 弹层默认挂到 body，scoped 样式够不着，这里用 rt- 前缀裸样式防串 -->
<style>
.rt-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 8px;
}

/* Teleport 进父页面容器（筛选卡按钮区/卡片标题行）时的内联形态 */
.rt-toolbar--inline {
  display: inline-flex;
  margin: 0;
}

.rt-colset-title {
  font-weight: 600;
  margin-bottom: 4px;
}

.rt-colset-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 8px;
}

.rt-colset-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 0;
}

.rt-colset-row .el-checkbox {
  flex: 1;
  margin-right: 0;
}

.rt-colset-ops {
  display: inline-flex;
  align-items: center;
}

.rt-colset-footer {
  margin-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 6px;
  text-align: right;
}
</style>
