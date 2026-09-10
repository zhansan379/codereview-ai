<template>
  <div class="kpi-tiles">
    <div v-for="r in rows" :key="r.label" class="kpi-tile">
      <div class="tile-val">
        {{ r.value }}<span v-if="r.hint" class="hint">{{ r.hint }}</span>
      </div>
      <div class="tile-label">
        <span class="dot" :style="{ background: r.color }"></span>{{ r.label }}
      </div>
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
/* 瓦片网格：2 列、行高 1fr → 无论几项都撑满卡片高度，不留底部空洞 */
.kpi-tiles {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  grid-auto-rows: 1fr;
  gap: 10px;
  height: 100%;
}
.kpi-tile {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: 6px;
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  min-width: 0;
}
.tile-val {
  font-size: 24px;
  font-weight: 700;
  line-height: 1;
}
.hint {
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
  margin-left: 4px;
}
.tile-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--el-text-color-regular);
  min-width: 0;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: none;
}
.empty {
  color: var(--el-text-color-placeholder);
  font-size: 13px;
  align-self: center;
}
</style>