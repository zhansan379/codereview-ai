<template>
  <div>
    <!-- 服务端过滤：筛选项 + 头部操作按钮 -->
    <el-card class="filter-card" data-tour="reviews-detail-filter">
      <template #header>
        <div class="card-head">
          <span class="card-title">{{ $t('reviews.filterTitle') }}</span>
          <span class="card-actions">
            <!-- 列设置齿轮由 ReviewsTable Teleport 过来，贴着现有按钮排，免得表格上方独占一行 -->
            <span id="reviews-table-tools" style="display: inline-flex"></span>
            <el-button @click="onRefresh">{{ $t('common.refresh') }}</el-button>
            <el-button type="success" :icon="Download" @click="openExport">
              {{ $t('reviews.exportExcel') }}
            </el-button>
          </span>
        </div>
      </template>
      <el-form inline class="filter-form" @submit.prevent>
        <el-form-item :label="$t('common.status')">
          <el-select
            v-model="query.state"
            :placeholder="$t('reviews.allStates')"
            clearable
            style="width: 160px"
            @change="onFilterChange"
          >
            <el-option
              v-for="o in stateOptions"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('reviews.provider')">
          <el-select
            v-model="query.provider"
            :placeholder="$t('reviews.allProviders')"
            clearable
            filterable
            allow-create
            default-first-option
            style="width: 130px"
            @change="onFilterChange"
          >
            <el-option v-for="p in providerOptions" :key="p" :label="p" :value="p" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('reviews.eventType')">
          <el-select
            v-model="query.event_type"
            :placeholder="$t('reviews.allEventTypes')"
            clearable
            style="width: 130px"
            @change="onFilterChange"
          >
            <el-option label="MR" value="mr" />
            <el-option label="PR" value="pr" />
            <el-option label="Push" value="push" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('reviews.score')">
          <el-input-number
            v-model="query.score_min"
            :min="0"
            :max="100"
            :controls="false"
            :placeholder="$t('reviews.scoreMin')"
            style="width: 90px"
            @change="onFilterChange"
          />
          <span class="score-sep">~</span>
          <el-input-number
            v-model="query.score_max"
            :min="0"
            :max="100"
            :controls="false"
            :placeholder="$t('reviews.scoreMax')"
            style="width: 90px"
            @change="onFilterChange"
          />
        </el-form-item>
        <el-form-item :label="$t('reviews.finishedAt')">
          <el-date-picker
            v-model="query.dateRange"
            type="daterange"
            range-separator="~"
            :start-placeholder="$t('reviews.dateStart')"
            :end-placeholder="$t('reviews.dateEnd')"
            value-format="YYYY-MM-DD"
            format="YYYY-MM-DD"
            unlink-panels
            style="width: 260px"
            @change="onFilterChange"
          />
        </el-form-item>
      </el-form>
    </el-card>

    <!-- Excel 导出：范围(当前筛选) + 问题过滤(严重度/状态) -->
    <el-dialog
      v-model="exportVisible"
      :title="$t('reviews.exportTitle')"
      width="480px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form label-width="auto">
        <el-form-item :label="$t('reviews.exportScope')">
          <span class="export-scope">
            {{ $t('reviews.exportScopeText') }}
            <el-tag v-if="query.state" size="small" type="info">{{ stateText }}</el-tag>
          </span>
        </el-form-item>
        <el-form-item :label="$t('reviews.severity')">
          <el-select
            v-model="exportForm.severities"
            multiple
            collapse-tags
            collapse-tags-tooltip
            clearable
            :placeholder="$t('reviews.allSeverities')"
            style="width: 100%"
          >
            <el-option v-for="o in severityOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('common.status')">
          <el-select
            v-model="exportForm.statuses"
            multiple
            collapse-tags
            collapse-tags-tooltip
            clearable
            :placeholder="$t('reviews.allStates')"
            style="width: 100%"
          >
            <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('reviews.exportContent')">
          <span class="export-scope">{{ $t('reviews.exportContentLine1') }}</span>
          <span class="export-scope">{{ $t('reviews.exportContentLine2') }}</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="exportVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="exporting" @click="doExport">
          {{ $t('reviews.exportSubmit') }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 多选批量操作条：勾选后才出现，按钮按所选行的状态自动适配生效范围 -->
    <el-card v-if="canManage && selection.length > 0" class="batch-card">
      <div class="batch-bar">
        <span class="batch-count">{{ $t('reviews.batchSelected', { n: selection.length }) }}</span>
        <span class="batch-actions">
          <el-button
            type="primary"
            size="small"
            :disabled="executableCount === 0"
            :loading="batchBusy"
            @click="onBatchExecute"
          >
            {{ $t('reviews.batchExecute') }} ({{ executableCount }})
          </el-button>
          <el-button
            type="warning"
            size="small"
            :disabled="stoppableCount === 0"
            :loading="batchBusy"
            @click="onBatchStop"
          >
            {{ $t('reviews.batchStop') }} ({{ stoppableCount }})
          </el-button>
          <el-button
            type="danger"
            size="small"
            :loading="batchBusy"
            @click="onBatchDelete"
          >
            {{ $t('reviews.batchDelete') }} ({{ selection.length }})
          </el-button>
        </span>
      </div>
    </el-card>

    <el-card data-tour="reviews-detail-table">
      <ReviewsTable
        :items="items"
        :loading="loading"
        storage-key="reviews"
        toolbar-target="#reviews-table-tools"
        show-process
        :show-retry="canManage"
        :show-stop="canManage"
        :selectable="canManage"
        :show-redeliver="canManage"
        :show-delete="canManage"
        :retrying-id="retryingId"
        :stopping-id="stoppingId"
        :redelivering-id="redeliveringId"
        @detail="goDetail"
        @retry="onRetry"
        @stop="onStop"
        @redeliver="onRedeliver"
        @delete="onDelete"
        @selection-change="onSelectionChange"
      />

      <!-- 服务端分页 -->
      <el-pagination
        class="pager"
        layout="total, prev, pager, next, sizes"
        :total="total"
        :page-size="query.limit"
        :current-page="query.offset / query.limit + 1"
        :page-sizes="[10, 20, 50]"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import {
  listReviews,
  retryTask,
  stopTask,
  redeliverTask,
  exportReviews,
  deleteReview,
  batchExecuteTasks,
  batchStopTasks,
  batchDeleteReviews,
  type ReviewFilter,
  type ReviewItem,
} from '../api'
import ReviewsTable from '../components/ReviewsTable.vue'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const { t } = useI18n()
const auth = useAuthStore()
// 写操作（重试/停止/重发/删除/批量）后端要求 reviews:manage；无权限时不渲染点了必 403 的按钮
const canManage = computed(() => auth.hasPerm('reviews:manage'))

const items = ref<ReviewItem[]>([])
const total = ref(0)
const loading = ref(false)
const retryingId = ref<number | null>(null)
const redeliveringId = ref<number | null>(null)
// 筛选条件持久化到 URL query，避免进入详情返回后丢失（DESIGN 精神：URL 即状态）。
const query = reactive<{
  state: string
  provider: string
  event_type: string
  score_min: number | null
  score_max: number | null
  dateRange: [string, string] | null
  limit: number
  offset: number
}>({
  state: '',
  provider: '',
  event_type: '',
  score_min: null,
  score_max: null,
  dateRange: null,
  limit: 10,
  offset: 0,
})
const providerOptions = ['gitlab', 'github', 'gitee']

// Excel 导出对话框状态
const exportVisible = ref(false)
const exporting = ref(false)
const exportForm = reactive<{ severities: string[]; statuses: string[] }>({
  severities: [],
  statuses: [],
})
// 与后端 Severity 枚举一致：仅 critical/high/medium/low 四档（error/warning/info 从不入库）。
// computed 而非常量：这些数组直接喂给 el-select，切语言后选项文案必须跟着变。
const severityOptions = computed(() =>
  (['critical', 'high', 'medium', 'low'] as const).map((v) => ({
    value: v,
    label: t(`enum.severity.${v}`),
  })),
)
const statusOptions = computed(() =>
  (['active', 'resolved'] as const).map((v) => ({
    value: v,
    label: t(`enum.findingStatus.${v}`),
  })),
)
// 状态选项须覆盖 DB 写入的状态全集（queued/running/completed/failed/skipped），
// 含瞬时态 running：配合批量停止等操作，用户需要筛出正在执行的任务
const stateOptions = computed(() =>
  (['queued', 'running', 'completed', 'skipped', 'failed'] as const).map((v) => ({
    value: v,
    label: t(`enum.state.${v}`),
  })),
)
const stateText = computed(
  () => stateOptions.value.find((o) => o.value === query.state)?.label || query.state,
)

function openExport() {
  exportForm.severities = []
  exportForm.statuses = []
  exportVisible.value = true
}

function pad(n: number) {
  return String(n).padStart(2, '0')
}
function ts() {
  const d = new Date()
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
}

// 顶层筛选字段（不含分页），列表与导出共用 → 所见即所导。
function filterFields(): Omit<ReviewFilter, 'limit' | 'offset'> {
  return {
    state: query.state || undefined,
    provider: query.provider || undefined,
    event_type: query.event_type || undefined,
    score_min: query.score_min ?? undefined,
    score_max: query.score_max ?? undefined,
    finished_from: query.dateRange ? `${query.dateRange[0]}T00:00:00` : undefined,
    finished_to: query.dateRange ? `${query.dateRange[1]}T23:59:59` : undefined,
  }
}

// 把当前筛选写入 URL query（保留 tab 等宿主参数），供进入详情返回后恢复。
// 本页管理的键先删再写：值清空/回到默认时要从 URL 摘掉，否则残留旧值，硬刷新会按旧值过滤/翻页。
function syncUrl() {
  const managed = [
    'state',
    'provider',
    'event_type',
    'score_min',
    'score_max',
    'finished_from',
    'finished_to',
    'limit',
    'offset',
  ]
  const q: Record<string, string | number> = { ...route.query }
  for (const k of managed) delete q[k]
  if (query.state) q.state = query.state
  if (query.provider) q.provider = query.provider
  if (query.event_type) q.event_type = query.event_type
  if (query.score_min != null) q.score_min = query.score_min
  if (query.score_max != null) q.score_max = query.score_max
  if (query.dateRange) {
    q.finished_from = query.dateRange[0]
    q.finished_to = query.dateRange[1]
  }
  if (query.limit !== 10) q.limit = query.limit
  if (query.offset > 0) q.offset = query.offset
  router.replace({ query: q })
}

function initFromRoute() {
  const r = route.query
  query.state = (r.state as string) || ''
  query.provider = (r.provider as string) || ''
  query.event_type = (r.event_type as string) || ''
  query.score_min = r.score_min != null ? Number(r.score_min) : null
  query.score_max = r.score_max != null ? Number(r.score_max) : null
  if (r.finished_from && r.finished_to) {
    query.dateRange = [r.finished_from as string, r.finished_to as string]
  }
  query.limit = r.limit ? Number(r.limit) : 10
  query.offset = r.offset ? Number(r.offset) : 0
}

async function doExport() {
  exporting.value = true
  try {
    const blob = await exportReviews({
      ...filterFields(),
      severities: exportForm.severities.length ? exportForm.severities : undefined,
      statuses: exportForm.statuses.length ? exportForm.statuses : undefined,
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${t('reviews.exportFileName')}_${ts()}.xlsx`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    exportVisible.value = false
    ElMessage.success(t('reviews.exportSuccess'))
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('reviews.exportFailed'))
  } finally {
    exporting.value = false
  }
}

// 翻页/过滤均重新请求后端（服务端分页）
async function load() {
  loading.value = true
  try {
    const res = await listReviews({
      ...filterFields(),
      limit: query.limit,
      offset: query.offset,
    })
    items.value = res.items || []
    total.value = res.total || 0
  } finally {
    loading.value = false
  }
}

function onFilterChange() {
  query.offset = 0
  syncUrl()
  load()
}
// 刷新仅重拉当前页数据：筛选与页码都不动
function onRefresh() {
  load()
}
function onPageChange(p: number) {
  query.offset = (p - 1) * query.limit
  syncUrl()
  load()
}
function onSizeChange(s: number) {
  query.limit = s
  query.offset = 0
  syncUrl()
  load()
}
function goDetail(id: number) {
  router.push(`/reviews/${id}`)
}

// 重试失败任务（合并自原任务页）：成功后刷新当前页
async function onRetry(row: ReviewItem) {
  retryingId.value = row.id
  try {
    await retryTask(row.id)
    ElMessage.success(t('reviews.retrySubmitted'))
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('reviews.retryFailed'))
  } finally {
    retryingId.value = null
  }
}

// ── 多选批量操作：执行（未开始/失败）、停止（排队中）、删除 ─────────────
const selection = ref<ReviewItem[]>([])
const stoppingId = ref<number | null>(null)
const batchBusy = ref(false)

function onSelectionChange(rows: ReviewItem[]) {
  selection.value = rows
}

// 各批量按钮只作用于状态匹配的所选行；括号里的计数让用户操作前就有预期
const executableCount = computed(
  () => selection.value.filter((r) => r.state === 'skipped' || r.state === 'failed').length,
)
const stoppableCount = computed(
  () => selection.value.filter((r) => r.state === 'queued').length,
)

// 把批量结果翻译成人话：「执行 3 条，2 条状态不符已跳过」；一条没执行成给 warning
function batchToast(prefix: string, executed: number, ignored: number, denied: number) {
  const parts = [t(`${prefix}.done`, { n: executed })]
  if (ignored) parts.push(t(`${prefix}.ignored`, { n: ignored }))
  if (denied) parts.push(t(`${prefix}.denied`, { n: denied }))
  const msg = parts.join('，')
  if (executed > 0) ElMessage.success(msg)
  else ElMessage.warning(msg)
}

async function onBatchExecute() {
  const ids = selection.value
    .filter((r) => r.state === 'skipped' || r.state === 'failed')
    .map((r) => r.id)
  if (!ids.length) return
  batchBusy.value = true
  try {
    const res = await batchExecuteTasks(ids)
    batchToast('reviews.batchExecuteResult', res.executed, res.ignored, res.denied)
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('reviews.batchFailed'))
  } finally {
    batchBusy.value = false
  }
}

async function onBatchStop() {
  const ids = selection.value.filter((r) => r.state === 'queued').map((r) => r.id)
  if (!ids.length) return
  batchBusy.value = true
  try {
    const res = await batchStopTasks(ids)
    batchToast('reviews.batchStopResult', res.executed, res.ignored, res.denied)
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('reviews.batchFailed'))
  } finally {
    batchBusy.value = false
  }
}

async function onBatchDelete() {
  const ids = selection.value.map((r) => r.id)
  try {
    await ElMessageBox.confirm(t('reviews.batchDeleteConfirm', { n: ids.length }), t('common.tip'), {
      type: 'warning',
    })
  } catch {
    return // 用户取消
  }
  batchBusy.value = true
  try {
    const res = await batchDeleteReviews(ids)
    if (res.denied) {
      ElMessage.warning(t('reviews.batchDeleteResult.done', { n: res.deleted })
        + '，' + t('reviews.batchDeleteResult.denied', { n: res.denied }))
    } else {
      ElMessage.success(t('reviews.batchDeleteResult.done', { n: res.deleted }))
    }
    if (items.value.length <= ids.length && query.offset > 0) {
      query.offset -= query.limit
      syncUrl()
    }
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.deleteFailed'))
  } finally {
    batchBusy.value = false
  }
}

// 停止单条排队中的任务：queued → 未开始（手动停止），残留队列项由后端幂等 no-op
async function onStop(row: ReviewItem) {
  stoppingId.value = row.id
  try {
    await stopTask(row.id)
    ElMessage.success(t('reviews.stopSubmitted'))
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('reviews.stopFailed'))
  } finally {
    stoppingId.value = null
  }
}

// 重新发送评论（writeback_failed 行）：从 DB 取已持久化成果补发，不重算。
// 后端 fire-and-forget 后台执行，成功与否在后台翻转 writeback_failed，这里仅提示已发起。
async function onRedeliver(row: ReviewItem) {
  redeliveringId.value = row.id
  try {
    await redeliverTask(row.id)
    ElMessage.success(t('reviews.redelivered'))
    load() // 刷新一下，防用户连续点击
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('reviews.redeliverFailed'))
  } finally {
    redeliveringId.value = null
  }
}

// 删除审查记录（含 findings 级联）：确认后删除并刷新；删空末页回退一页。
async function onDelete(id: number) {
  try {
    await ElMessageBox.confirm(t('reviews.deleteConfirm', { id }), t('common.tip'), {
      type: 'warning',
    })
  } catch {
    return // 用户取消
  }
  try {
    await deleteReview(id)
    ElMessage.success(t('common.deleted'))
    if (items.value.length === 1 && query.offset > 0) {
      query.offset -= query.limit
      syncUrl()
    }
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.deleteFailed'))
  }
}

onMounted(() => {
  initFromRoute()
  load()
})
</script>

<style scoped>
.filter-card {
  margin-bottom: 5px;
}
.batch-card {
  margin-bottom: 5px;
}
.batch-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}
.batch-count {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.batch-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.card-title {
  font-weight: 600;
  font-size: 15px;
}
.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.filter-form {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  row-gap: 12px;
  column-gap: 24px;
}
.filter-form .el-form-item {
  margin: 0;
}
.filter-form .el-form-item__label {
  color: var(--el-text-color-regular);
}
.score-sep {
  margin: 0 6px;
  color: var(--el-text-color-secondary);
}
.export-scope {
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.6;
}
.export-scope + .export-scope {
  display: block;
}
.pager {
  margin-top: 16px;
  justify-content: flex-end;
}
</style>