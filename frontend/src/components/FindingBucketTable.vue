<script setup lang="ts">
// 四桶共用的紧凑 finding 表（MR 汇总视图 ReviewPrsView 共用）。
// 直接渲染后端 `bucket_compare` 平铺的 finding dict，不共享详情页扩展表头。
import type { CompareBucketItem } from '../api'

defineProps<{
  rows: CompareBucketItem[]
  empty?: string
  severityTag?: string
}>()

function sevTag(s: string): string {
  if (s === 'critical' || s === 'high' || s === 'error') return 'danger'
  if (s === 'medium' || s === 'warning') return 'warning'
  return 'info'
}
</script>

<template>
  <div v-if="!rows.length" class="empty-row">{{ empty }}</div>
  <table v-else class="bucket-table">
    <thead>
      <tr>
        <th class="sev">严重度</th>
        <th class="file">文件</th>
        <th class="line">行</th>
        <th class="cnt">问题内容</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="(r, i) in rows" :key="r.file + ':' + r.content + ':' + i">
        <td class="sev">
          <el-tag :type="sevTag(r.severity)" size="small">{{ r.severity }}</el-tag>
        </td>
        <td class="file">{{ r.file }}</td>
        <td class="line">{{ r.line ?? r.old_line ?? '-' }}</td>
        <td class="cnt">{{ r.content }}</td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
.bucket-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
.bucket-table th,
.bucket-table td {
  border-bottom: 1px solid #f0f2f5;
  padding: 6px 8px;
  text-align: left;
  vertical-align: top;
}
.bucket-table th {
  color: #909399;
  font-weight: 600;
}
.sev {
  width: 70px;
  white-space: nowrap;
}
.file {
  width: 28%;
  word-break: break-all;
}
.line {
  width: 46px;
  color: #909399;
  white-space: nowrap;
}
.cnt {
  white-space: pre-wrap;
  word-break: break-word;
}
.empty-row {
  color: #909399;
  font-size: 12.5px;
  padding: 8px 4px;
}
</style>