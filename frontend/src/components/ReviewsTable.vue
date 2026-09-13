<template>
  <el-table :data="items" v-loading="loading" stripe @selection-change="onSelectionChange">
    <el-table-column v-if="selectable" type="selection" width="42" />
    <el-table-column prop="id" label="ID" width="50" />
    <el-table-column prop="pr_number" label="PR" width="50" />
    <el-table-column prop="pr_title" :label="$t('reviewsTable.title')" min-width="180" show-overflow-tooltip />
    <el-table-column :label="$t('reviewsTable.author')" width="120" show-overflow-tooltip>
      <template #default="{ row }">{{ row.pr_author || '—' }}</template>
    </el-table-column>
    <el-table-column prop="provider" :label="$t('reviewsTable.provider')" :width="colWidth(80)" />
    <el-table-column prop="repo_id" :label="$t('reviewsTable.repoId')" min-width="200" show-overflow-tooltip />
    <el-table-column v-if="showProcess" prop="event_type" :label="$t('reviewsTable.event')" :width="colWidth(60)" />
    <el-table-column v-if="showProcess" prop="branch" :label="$t('reviewsTable.branch')" width="200" show-overflow-tooltip />
    <el-table-column v-if="showProcess" prop="attempt" :label="$t('reviewsTable.attempt')" :width="colWidth(80)" />
    <el-table-column prop="score_total" :label="$t('reviewsTable.score')" :width="colWidth(60)" />
    <el-table-column :label="$t('reviewsTable.mode')" width="80">
      <template #default="{ row }">
        <el-tag :type="modeTagType(row.exec_mode)" size="small">{{ modeLabel(row.exec_mode) }}</el-tag>
      </template>
    </el-table-column>
    <el-table-column :label="$t('reviewsTable.state')" width="100">
      <template #default="{ row }">
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
    </el-table-column>
    <el-table-column :label="timeLabel" :width="160">
      <template #default="{ row }">{{ formatTime(row[timeField]) }}</template>
    </el-table-column>
    <!-- 宽度按最挤的一行给：详情 + 删除 + 补审/重试 + 重新发送 四个按钮同时出现时，
         中文需 ~200px（含按钮间距 12px×3 与单元格内边距 24px），英文需 ~238px。
         原先给的 160px 中文就会折行，把行高撑高——这里一并修掉。 -->
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
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { QuestionFilled } from '@element-plus/icons-vue'
import { formatTime, stateTagType, stateLabel, skipReasonLabel, modeLabel, modeTagType } from '../utils/format'
import type { ReviewItem } from '../api'
import { colWidth } from '../composables/useLocale'

// 审查记录 / 仪表盘"最近记录"共用的表格：列统一，改动一处多处生效。
// - timeField 决定显示"排队时间"还是"完成时间"（标签与列宽随之切换）。
// - showProcess 控制是否显示"事件类型/分支/尝试次数"过程列（审查记录页合并任务后开启）。
// - showRetry 控制"重试/执行"按钮（failed 行重试；skipped=未开始行执行），配合 retryingId 显示该行按钮 loading。
// - showStop 控制"停止"按钮（仅 queued 行；排队中 → 未开始），配合 stoppingId 显示 loading。
// - selectable 开启多选列（批量操作用），勾选变化经 selection-change 抛给父级。
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

const timeLabel = computed(() =>
  props.timeField === 'finished_at' ? t('reviewsTable.finishedAt') : t('reviewsTable.queuedAt'),
)

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
