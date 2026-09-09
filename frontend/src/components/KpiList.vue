<template>
  <div class="kpi-list">
    <div v-for="r in rows" :key="r.label" class="kpi-row">
      <span class="dot" :style="{ background: r.color }"></span>
      <span class="label">{{ r.label }}</span>
      <span class="val">
        {{ r.value }}<span v-if="r.hint" class="hint">{{ r.hint }}</span>
      </span>
    </div>
    <div v-if="!rows.length" class="empty">暂无数据</div>
  </div>
</template>

<script setup lang="ts">
export interface KpiRow {
  label: string
  value: string | number
  color?: string
  hint?: string // 次级说明，如百分比
}

defineProps<{ rows: KpiRow[] }>()
</script>

<style scoped>
.kpi-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.kpi-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex: none;
}
.label {
  color: #606266;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.val {
  font-size: 22px;
  font-weight: 700;
  line-height: 1;
}
.hint {
  font-size: 12px;
  font-weight: 400;
  color: #909399;
  margin-left: 4px;
}
.empty {
  color: #c0c4cc;
  font-size: 13px;
}
</style>