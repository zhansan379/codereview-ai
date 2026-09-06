<template>
  <div v-loading="loading">
    <el-page-header @back="$router.back()" :content="`审查详情 #${id}`" />

    <el-card v-if="detail" class="info-card">
      <el-descriptions :column="3" border>
        <el-descriptions-item label="ID">{{ detail.id }}</el-descriptions-item>
        <el-descriptions-item label="平台">{{ detail.provider }}</el-descriptions-item>
        <el-descriptions-item label="仓库 ID">{{ detail.repo_id }}</el-descriptions-item>
        <el-descriptions-item label="PR 号">{{ detail.pr_number }}</el-descriptions-item>
        <el-descriptions-item label="事件类型">{{ detail.event_type }}</el-descriptions-item>
        <el-descriptions-item label="分支">{{ detail.branch }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="stateTag(detail.state)">{{ detail.state }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="评分">{{ detail.score_total }}</el-descriptions-item>
        <el-descriptions-item label="Trace ID">{{ detail.trace_id }}</el-descriptions-item>
      </el-descriptions>
      <div v-if="detail.error" class="error-box">
        <b>错误信息：</b>{{ detail.error }}
      </div>
    </el-card>

    <el-card class="info-card">
      <template #header>总结（Markdown）</template>
      <pre class="summary">{{ detail?.summary_md || '（无总结）' }}</pre>
    </el-card>

    <el-card>
      <template #header>发现的问题（{{ detail?.findings?.length ?? 0 }}）</template>
      <el-table :data="detail?.findings || []" stripe>
        <el-table-column prop="severity" label="严重度" width="90">
          <template #default="{ row }">
            <el-tag :type="severityTag(row.severity)">{{ row.severity }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="category" label="类别" width="120" />
        <el-table-column prop="file" label="文件" min-width="160" />
        <el-table-column prop="new_line" label="行号" width="80" />
        <el-table-column prop="title" label="标题" min-width="200" />
        <el-table-column prop="status" label="状态" width="100" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { getReview, type ReviewDetail } from '../api'

const route = useRoute()
const id = Number(route.params.id)
const detail = ref<ReviewDetail | null>(null)
const loading = ref(false)

function stateTag(state: string): any {
  if (state === 'reviewed' || state === 'success') return 'success'
  if (state === 'failed') return 'danger'
  return 'warning'
}
function severityTag(sev: string): any {
  if (sev === 'critical' || sev === 'high' || sev === 'error') return 'danger'
  if (sev === 'medium' || sev === 'warning') return 'warning'
  return 'info'
}

async function load() {
  loading.value = true
  try {
    detail.value = await getReview(id)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.info-card {
  margin: 16px 0;
}
.error-box {
  margin-top: 16px;
  color: #f56c6c;
}
.summary {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
</style>