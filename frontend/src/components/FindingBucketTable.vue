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
        <th class="sev">{{ $t('findingBucket.col.severity') }}</th>
        <th class="file">{{ $t('findingBucket.col.file') }}</th>
        <th class="line">{{ $t('findingBucket.col.line') }}</th>
        <th class="cnt">{{ $t('findingBucket.col.content') }}</th>
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
  border-bottom: 1px solid var(--el-border-color-lighter);
  padding: 6px 8px;
  text-align: left;
  vertical-align: top;
}
.bucket-table th {
  color: var(--el-text-color-secondary);
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
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.cnt {
  white-space: pre-wrap;
  word-break: break-word;
}
.empty-row {
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
  padding: 8px 4px;
}
</style>