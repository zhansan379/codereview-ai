<template>
  <el-table :data="items" v-loading="loading" stripe>
    <el-table-column prop="id" label="ID" width="70" />
    <el-table-column prop="provider" label="平台" width="80" />
    <el-table-column prop="repo_id" label="仓库 ID" min-width="110" />
    <el-table-column prop="pr_number" label="PR" width="70" />
    <el-table-column prop="score_total" label="评分" width="80" />
    <el-table-column label="状态" width="100">
      <template #default="{ row }">
        <el-tag :type="stateTagType(row.state)">{{ stateLabel(row.state) }}</el-tag>
      </template>
    </el-table-column>
    <el-table-column :label="timeLabel" :width="timeWidth">
      <template #default="{ row }">{{ formatTime(row[timeField]) }}</template>
    </el-table-column>
    <el-table-column v-if="showAction" label="操作" width="90" fixed="right">
      <template #default="{ row }">
        <el-button link type="primary" @click="$emit('detail', row.id)">详情</el-button>
      </template>
    </el-table-column>
  </el-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { formatTime, stateTagType, stateLabel } from '../utils/format'
import type { ReviewItem } from '../api'

// 审查记录 / 仪表盘"最近记录"共用的表格：列统一，改动一处两处生效。
// timeField 决定显示"排队时间"还是"完成时间"（标签与列宽随之切换）。
const props = withDefaults(
  defineProps<{
    items: ReviewItem[]
    loading?: boolean
    /** 是否显示"详情"操作列（审查记录页显示，仪表盘摘要可关闭） */
    showAction?: boolean
    /** 展示哪个时间字段：排队时间或完成时间 */
    timeField?: 'queued_at' | 'finished_at'
  }>(),
  {
    loading: false,
    showAction: true,
    timeField: 'queued_at',
  },
)

defineEmits<{ (e: 'detail', id: number): void }>()

const timeLabel = computed(() => (props.timeField === 'finished_at' ? '完成时间' : '排队时间'))
const timeWidth = computed(() => (props.timeField === 'finished_at' ? 360 : 300))
</script>