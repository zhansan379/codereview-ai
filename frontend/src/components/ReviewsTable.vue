<template>
  <el-table :data="items" v-loading="loading" stripe>
    <el-table-column prop="id" label="ID" width="50" />
    <el-table-column prop="pr_number" label="PR" width="50" />
    <el-table-column prop="pr_title" :label="$t('reviewsTable.title')" min-width="180" show-overflow-tooltip />
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
        <el-tag :type="stateTagType(row.state)">{{ stateLabel(row.state) }}</el-tag>
      </template>
    </el-table-column>
    <el-table-column :label="timeLabel" :width="160">
      <template #default="{ row }">{{ formatTime(row[timeField]) }}</template>
    </el-table-column>
    <!-- 宽度按最挤的一行给：详情 + 删除 + 补审/重试 + 重新发送 四个按钮同时出现时，
         中文需 ~200px（含按钮间距 12px×3 与单元格内边距 24px），英文需 ~238px。
         原先给的 160px 中文就会折行，把行高撑高——这里一并修掉。 -->
    <el-table-column v-if="showAction || showRetry || showDelete" :label="$t('common.actions')" :width="colWidth(showDelete ? 205 : 120)"
      fixed="right">
      <template #default="{ row }">
        <el-button v-if="showAction" link type="primary" @click="$emit('detail', row.id)">{{ $t('common.detail') }}</el-button>
        <el-button v-if="showDelete" link type="danger" @click="$emit('delete', row.id)">{{ $t('common.delete') }}</el-button>
        <el-button v-if="showRetry && isRetryable(row)" link type="danger" :loading="retryingId === row.id"
          @click="$emit('retry', row)">{{ row.state === 'skipped' ? $t('reviewsTable.reReview') : $t('reviewsTable.retry') }}</el-button>
        <el-button v-if="showRedeliver && row.writeback_failed" link type="warning" :loading="redeliveringId === row.id"
          @click="$emit('redeliver', row)">{{ $t('reviewsTable.resend') }}</el-button>
      </template>
    </el-table-column>
  </el-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatTime, stateTagType, stateLabel, modeLabel, modeTagType } from '../utils/format'
import type { ReviewItem } from '../api'
import { colWidth } from '../composables/useLocale'

// 审查记录 / 仪表盘"最近记录"共用的表格：列统一，改动一处多处生效。
// - timeField 决定显示"排队时间"还是"完成时间"（标签与列宽随之切换）。
// - showProcess 控制是否显示"事件类型/分支/尝试次数"过程列（审查记录页合并任务后开启）。
// - showRetry 控制"重试/补审"按钮（failed 或可补审的 push skipped 行），配合 retryingId 显示该行按钮 loading。
const props = withDefaults(
  defineProps<{
    items: ReviewItem[]
    loading?: boolean
    /** 是否显示"详情"按钮 */
    showAction?: boolean
    /** 是否显示"事件类型/分支/尝试次数"过程列（审查记录页开启；仪表盘精简关） */
    showProcess?: boolean
    /** 是否显示"重试"按钮（仅 failed 行；审查记录页开启；仪表盘关） */
    showRetry?: boolean
    /** 是否显示"删除"按钮（审查记录页开启；仪表盘共用表格关，默认关） */
    showDelete?: boolean
    /** 是否显示"重新发送"按钮（仅 writeback_failed 行；审查记录页开启） */
    showRedeliver?: boolean
    /** 当前正在重试的行 id，用于锁住对应"重试"按钮的 loading 态 */
    retryingId?: number | null
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
    showRedeliver: false,
    showDelete: false,
    retryingId: null,
    redeliveringId: null,
    timeField: 'queued_at',
  },
)

defineEmits<{
  (e: 'detail', id: number): void
  (e: 'retry', row: ReviewItem): void
  (e: 'redeliver', row: ReviewItem): void
  (e: 'delete', id: number): void
}>()

const { t } = useI18n()

const timeLabel = computed(() =>
  props.timeField === 'finished_at' ? t('reviewsTable.finishedAt') : t('reviewsTable.queuedAt'),
)

// 该行是否可重试/补审：failed 可重试；skipped 门控/配置类可补审（绕过门控）。
// 与后端 _retryable()（api/admin/tasks.py）完全一致：push 轨 push_disabled/branch_mismatch；
// mr 轨 mr_disabled（MR 无分支规则）；branch_deleted 无 head 可审禁止。
const RETRYABLE_PUSH_REASONS = ['push_disabled', 'branch_mismatch']
const RETRYABLE_MR_REASONS = ['mr_disabled']

function isRetryable(row: ReviewItem): boolean {
  if (row.state === 'failed') return true
  if (row.state !== 'skipped') return false
  const reasons =
    row.event_type === 'push'
      ? RETRYABLE_PUSH_REASONS
      : row.event_type === 'mr'
        ? RETRYABLE_MR_REASONS
        : []
  return reasons.includes(row.skip_reason || '')
}
</script>