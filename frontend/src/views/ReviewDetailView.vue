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
          <el-tag :type="stateTagType(detail.state)">{{ stateLabel(detail.state) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="评分">{{ detail.score_total }}</el-descriptions-item>
        <el-descriptions-item label="提交 SHA">
          <code class="sha">{{ detail.head_sha }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="排队时间">{{ formatTime(detail.queued_at) }}</el-descriptions-item>
        <el-descriptions-item label="完成时间">
          {{ detail.finished_at ? formatTime(detail.finished_at) : '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="Trace ID">{{ detail.trace_id || '—' }}</el-descriptions-item>
      </el-descriptions>

      <!-- 失败原因分析 -->
      <div v-if="detail.state === 'failed'" class="analysis analysis-fail">
        <b>失败原因分析</b>
        <pre class="analysis-text">{{ detail.error || '（未知）' }}</pre>
      </div>

      <!-- 跳过原因 -->
      <div v-if="detail.state === 'skipped'" class="analysis analysis-skip">
        <b>跳过原因</b>
        <pre class="analysis-text">{{ detail.error || 'push 审查未开启或该分支未命中规则，仅记录未审查。' }}</pre>
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
import { formatTime, stateTagType, stateLabel } from '../utils/format'

const route = useRoute()
const id = Number(route.params.id)
const detail = ref<ReviewDetail | null>(null)
const loading = ref(false)

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
.sha {
  font-size: 12px;
}
.analysis {
  margin-top: 16px;
  padding: 12px 16px;
  border-radius: 6px;
}
.analysis-fail {
  border: 1px solid #fbc4c4;
  background: #fef0f0;
}
.analysis-skip {
  border: 1px solid #d3dce6;
  background: #f4f4f5;
}
.analysis-text {
  margin: 8px 0 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.6;
}
.summary {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
</style>