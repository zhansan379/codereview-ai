<template>
  <el-table :data="items" v-loading="loading" stripe>
    <el-table-column prop="id" label="ID" width="50" />
    <el-table-column prop="pr_number" label="PR" width="50" />
    <el-table-column prop="pr_title" label="标题" min-width="180" show-overflow-tooltip />
    <el-table-column prop="provider" label="平台" width="80" />
    <el-table-column prop="repo_id" label="仓库 ID" min-width="200" show-overflow-tooltip />
    <el-table-column v-if="showProcess" prop="event_type" label="事件类型" width="80" />
    <el-table-column
        v-if="showProcess"
        prop="branch"
        label="分支"
        width="240"
        show-overflow-tooltip
      />
    <el-table-column v-if="showProcess" prop="attempt" label="尝试次数" width="80" />
    <el-table-column prop="score_total" label="评分" width="60" />
    <el-table-column label="状态" width="100">
      <template #default="{ row }">
        <el-tag :type="stateTagType(row.state)">{{ stateLabel(row.state) }}</el-tag>
      </template>
    </el-table-column>
    <el-table-column :label="timeLabel" :width="timeWidth">
      <template #default="{ row }">{{ formatTime(row[timeField]) }}</template>
    </el-table-column>
    <el-table-column v-if="showAction || showRetry" label="操作" width="120" fixed="right">
      <template #default="{ row }">
        <el-button v-if="showAction" link type="primary" @click="$emit('detail', row.id)">详情</el-button>
        <el-button
          v-if="showRetry && isRetryable(row)"
          link
          type="danger"
          :loading="retryingId === row.id"
          @click="$emit('retry', row)"
        >{{ row.state === 'skipped' ? '补审' : '重试' }}</el-button>
      </template>
    </el-table-column>
  </el-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { formatTime, stateTagType, stateLabel } from '../utils/format'
import type { ReviewItem } from '../api'

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
    /** 当前正在重试的行 id，用于锁住对应"重试"按钮的 loading 态 */
    retryingId?: number | null
    /** 展示哪个时间字段：排队时间或完成时间 */
    timeField?: 'queued_at' | 'finished_at'
  }>(),
  {
    loading: false,
    showAction: true,
    showProcess: false,
    showRetry: false,
    retryingId: null,
    timeField: 'queued_at',
  },
)

defineEmits<{ (e: 'detail', id: number): void; (e: 'retry', row: ReviewItem): void }>()

const timeLabel = computed(() => (props.timeField === 'finished_at' ? '完成时间' : '排队时间'))
const timeWidth = computed(() => (props.timeField === 'finished_at' ? 360 : 300))

// 该行是否可重试/补审：failed 可重试；push 轨门控/配置类 skipped 可补审。
// 删分支（无 head 可审）禁止。旧数据 skip_reason 为空时保守依赖 error 文本。
function isRetryable(row: ReviewItem): boolean {
  if (row.state === 'failed') return true
  if (row.state !== 'skipped') return false
  if (row.event_type !== 'push') return false
  if (row.skip_reason === 'branch_deleted') return false
  if (row.skip_reason) return true
  return !(row.error || '').includes('删除分支')
}
</script>