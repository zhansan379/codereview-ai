<template>
  <div>
    <el-page-header @back="$router.back()" :content="`对比上次审查 #${id}`" />

    <div v-loading="loading">
      <div class="bucket-grid">
        <el-card class="bucket bucket-new" shadow="never">
          <template #header>
            <div class="bucket-head">
              <el-tag type="danger">新增</el-tag>
              <span class="count">{{ result.new.length }}</span>
            </div>
          </template>
          <FindingTable :rows="result.new" empty="本轮无新增问题" severity-tag="danger" />
        </el-card>

        <el-card class="bucket bucket-resolved" shadow="never">
          <template #header>
            <div class="bucket-head">
              <el-tag type="success">已解决</el-tag>
              <span class="count">{{ result.resolved.length }}</span>
            </div>
          </template>
          <FindingTable :rows="result.resolved" empty="本轮已全部修复" severity-tag="success" />
        </el-card>

        <el-card class="bucket bucket-persisting" shadow="never">
          <template #header>
            <div class="bucket-head">
              <el-tag type="warning">持续存在</el-tag>
              <span class="count">{{ result.persisting.length }}</span>
            </div>
          </template>
          <FindingTable :rows="result.persisting" empty="无持续存在的问题" severity-tag="warning" />
        </el-card>

        <el-card class="bucket bucket-not-reviewed" shadow="never">
          <template #header>
            <div class="bucket-head">
              <el-tag type="info">上次未覆盖（不算已修）</el-tag>
              <span class="count">{{ result.not_reviewed.length }}</span>
            </div>
          </template>
          <FindingTable :rows="result.not_reviewed" empty="无" severity-tag="info" />
        </el-card>
      </div>

      <el-alert
        type="info"
        :closable="false"
        title="口径说明"
        description="对比取本任务与上一次 completed MR 任务的 findings 快照做指纹差。『已解决』指上次有、本轮确实审到（在覆盖集内）且不复现；『上次未覆盖』指问题仍在但本轮未审到该文件（未变更复用/缺失覆盖集时保守登记），不代表已修复。"
        style="margin-top: 16px"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { defineComponent, h, ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { fetchReviewCompare, type CompareResult, type CompareBucketItem } from '../api'

// 直接把平铺 finding dict 渲染成紧凑表（不共享详情页扩展的表头，保持桶页简洁）。
const FindingTable = defineComponent({
  props: {
    rows: { type: Array as () => CompareBucketItem[], default: () => [] },
    empty: { type: String, default: '空' },
    severityTag: { type: String, default: 'info' },
  },
  setup(props) {
    const sevTag = (s: string): any => {
      if (s === 'critical' || s === 'high' || s === 'error') return 'danger'
      if (s === 'medium' || s === 'warning') return 'warning'
      return 'info'
    }
    return () => {
      if (!props.rows.length) {
        return h('div', { class: 'empty-row' }, props.empty)
      }
      const rows = props.rows.map((r) =>
        h('tr', { key: r.file + ':' + r.content }, [
          h('td', { class: 'sev' }, h('el-tag', { type: sevTag(r.severity), size: 'small' }, r.severity)),
          h('td', { class: 'file' }, r.file),
          h('td', { class: 'line' }, r.line ?? r.old_line ?? '-'),
          h('td', { class: 'cnt' }, r.content),
        ])
      )
      return h('table', { class: 'bucket-table' }, [
        h('thead', null, h('tr', null, [
          h('th', null, '严重度'),
          h('th', null, '文件'),
          h('th', null, '行'),
          h('th', null, '问题内容'),
        ])),
        h('tbody', null, rows),
      ])
    }
  },
})

const route = useRoute()
const id = Number(route.params.id)
const result = ref<CompareResult>({ new: [], persisting: [], resolved: [], not_reviewed: [] })
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    result.value = await fetchReviewCompare(id)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.bucket-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px;
  margin-top: 16px;
  align-items: start;
}
.bucket-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.count {
  font-size: 13px;
  color: #606266;
}
.bucket :deep(.el-card__body) {
  padding: 8px 12px;
}
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