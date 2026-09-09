<template>
  <div v-loading="loading">
    <el-page-header @back="$router.back()" :content="`审查详情 #${id}`" />

    <div v-if="detail" class="review-toolbar">
      <el-button type="primary" plain :disabled="busy || loading" @click="copyMarkdown">
        <el-icon><DocumentCopy /></el-icon>&nbsp;复制完整 Markdown
      </el-button>
      <span class="toolbar-hint">可直接粘贴到 agent 平台用于修复问题</span>
    </div>

    <el-card v-if="detail" class="info-card">
      <el-descriptions :column="3" border>
        <el-descriptions-item label="标题">{{ detail.event_type === 'push' ? '—' : (detail.pr_title || '—') }}</el-descriptions-item>
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
        <el-descriptions-item label="变更行数">{{ detail.diff_lines }} 行</el-descriptions-item>
        <el-descriptions-item label="提交 SHA">
          <code class="sha">{{ detail.head_sha }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="比对基线">
          <code class="sha">{{ detail.base_sha || '—' }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="排队时间">{{ formatTime(detail.queued_at) }}</el-descriptions-item>
        <el-descriptions-item label="完成时间">
          {{ detail.finished_at ? formatTime(detail.finished_at) : '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="Trace ID">{{ detail.trace_id || '—' }}</el-descriptions-item>
        <el-descriptions-item v-if="detail.web_url" label="直达链接">
          <el-link :href="detail.web_url" target="_blank" type="primary">
            {{ detail.event_type === 'push' ? '打开提交' : '打开 MR/PR' }}
          </el-link>
        </el-descriptions-item>
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

      <!-- agentic 原始对话 / 前后增量对比入口 -->
      <div v-if="detail.event_type === 'mr'" class="agent-links">
        <el-button type="primary" plain @click="$router.push(`/reviews/${id}/conversation`)">
          <el-icon><ChatDotRound /></el-icon>&nbsp;原始对话
        </el-button>
        <el-button type="success" plain @click="$router.push(`/reviews/${id}/compare`)">
          <el-icon><DataAnalysis /></el-icon>&nbsp;对比上次
        </el-button>
      </div>
    </el-card>

    <!-- push 轨：提交消息（多行）单独展示，不当标题 -->
    <el-card v-if="detail?.event_type === 'push' && detail.push_commits" class="info-card">
      <template #header>推送提交</template>
      <pre class="summary summary-commits">{{ detail.push_commits }}</pre>
    </el-card>

    <el-card class="info-card">
      <template #header>总结（Markdown）</template>
      <pre class="summary">{{ detail?.summary_md || '（无总结）' }}</pre>
    </el-card>

    <el-card>
      <template #header>发现的问题（{{ detail?.findings?.length ?? 0 }}）</template>
      <el-table :data="detail?.findings || []" stripe>
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="finding-detail">
              <div class="finding-actions">
                <span v-if="row.first_seen" class="finding-meta">首次 {{ formatTime(row.first_seen) }}</span>
                <span v-if="row.status === 'resolved' && row.last_seen" class="finding-meta">解决 {{ formatTime(row.last_seen) }}</span>
              </div>
              <div v-if="row.existing_code" class="code-block">
                <div class="code-label">原代码</div>
                <pre class="code-text">{{ row.existing_code }}</pre>
              </div>
              <div v-if="row.suggestion" class="code-block">
                <div class="code-label code-fix">建议修复</div>
                <pre class="code-text">{{ row.suggestion }}</pre>
              </div>
              <div v-if="row.detail" class="code-block">
                <div class="code-label">详细分析</div>
                <pre class="code-text">{{ row.detail }}</pre>
              </div>
              <el-empty v-if="!row.existing_code && !row.suggestion" description="无代码片段" :image-size="40" />
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="severity" label="严重度" width="100">
          <template #default="{ row }">
            <el-tag :type="severityTag(row.severity)">{{ row.severity }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="category" label="类别" width="110" />
        <el-table-column prop="file" label="文件" min-width="150" />
        <el-table-column prop="new_line" label="行号" width="70" />
        <el-table-column prop="title" label="分析" min-width="220" show-overflow-tooltip />
        <el-table-column prop="source" label="来源" width="90">
          <template #default="{ row }">
            <el-tag :type="sourceTagType(row.source)" size="small">{{ sourceLabel(row.source) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="findingStatusTag(row.status)" size="small">
              {{ statusLabel(row.status) }}
              <span v-if="row.status === 'active' && row.reopened_count > 0"> 重开×{{ row.reopened_count }}</span>
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'active'"
              type="warning"
              size="small"
              plain
              link
              :disabled="busy"
              @click="onFindingStatus(row, 'waived')"
            >搁置</el-button>
            <el-button
              v-if="row.status === 'waived'"
              type="primary"
              size="small"
              plain
              link
              :disabled="busy"
              @click="onFindingStatus(row, 'active')"
            >恢复</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ChatDotRound, DataAnalysis, DocumentCopy } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { getReview, setFindingStatus, type ReviewDetail, type ReviewFinding } from '../api'
import { formatTime, stateTagType, stateLabel } from '../utils/format'

const route = useRoute()
const id = Number(route.params.id)
const detail = ref<ReviewDetail | null>(null)
const loading = ref(false)
const busy = ref(false)

function findingStatusTag(status: string | null): any {
  if (status === 'resolved') return 'success'
  if (status === 'waived') return 'warning'
  return 'info'
}

function statusLabel(status: string | null): string {
  if (status === 'resolved') return '已解决'
  if (status === 'waived') return '已搁置'
  return '待处理'
}

function severityTag(sev: string): any {
  if (sev === 'critical' || sev === 'high' || sev === 'error') return 'danger'
  if (sev === 'medium' || sev === 'warning') return 'warning'
  return 'info'
}

function sourceLabel(source: string | null): string {
  if (!source) return '未知'
  if (source === 'llm') return 'LLM'
  if (source.startsWith('static')) return '静态分析'
  return source
}

function sourceTagType(source: string | null): any {
  if (source === 'llm') return 'primary'
  if (source && source.startsWith('static')) return 'warning'
  return 'info'
}

function fence(code: string | null): string {
  if (!code) return ''
  return '```\n' + code + '\n```'
}

function buildMarkdown(d: ReviewDetail): string {
  const L: string[] = []
  L.push(`# 代码审查报告 #${d.id}`)
  L.push('')
  L.push('## 元信息')
  L.push(`- 平台：${d.provider || '—'}`)
  L.push(`- 仓库 ID：${d.repo_id || '—'}`)
  L.push(`- 事件类型：${d.event_type || '—'}`)
  if (d.pr_number != null) L.push(`- PR/MR 号：${d.pr_number}`)
  if (d.pr_title) L.push(`- 标题：${d.pr_title}`)
  if (d.branch) L.push(`- 分支：${d.branch}`)
  if (d.head_sha) L.push(`- 提交 SHA：${d.head_sha}`)
  if (d.base_sha) L.push(`- 比对基线：${d.base_sha}`)
  L.push(`- 评分：${d.score_total ?? '—'}`)
  if (d.diff_lines != null) L.push(`- 变更行数：${d.diff_lines} 行`)
  if (d.web_url) L.push(`- 直达链接：${d.web_url}`)
  L.push('')

  L.push('## 总结')
  L.push(d.summary_md || '（无总结）')
  L.push('')

  const findings = d.findings || []
  L.push(`## 发现的问题（${findings.length}）`)
  if (findings.length === 0) {
    L.push('未发现需要处理的问题。')
  } else {
    findings.forEach((f, i) => {
      const sev = f.severity || 'unknown'
      L.push(`### [${i + 1}] ${sev} · ${f.category || '未分类'}：${f.title}`)
      L.push('')
      L.push(`- 文件：\`${f.file}\``)
      L.push(`- 行号：${f.new_line ?? '—'}`)
      L.push(`- 来源：${f.source || '未知'}`)
      L.push(`- 状态：${statusLabel(f.status)}`)
      if (f.existing_code) {
        L.push('')
        L.push('**原代码：**')
        L.push(fence(f.existing_code))
      }
      if (f.suggestion) {
        L.push('')
        L.push('**建议修复：**')
        L.push(fence(f.suggestion))
      }
      if (f.detail) {
        L.push('')
        L.push('**详细分析：**')
        L.push(f.detail)
      }
      L.push('')
      L.push('---')
      L.push('')
    })
  }
  return L.join('\n').replace(/^\n+/, '').replace(/\n+$/, '')
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
    return
  }
  // 非 HTTPS / 旧浏览器降级
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  const ok = document.execCommand('copy')
  document.body.removeChild(ta)
  if (!ok) throw new Error('copy failed')
}

async function copyMarkdown() {
  if (!detail.value) return
  try {
    await copyText(buildMarkdown(detail.value))
    ElMessage.success('已复制完整 Markdown')
  } catch (e: any) {
    ElMessage.error('复制失败，请手动选择文本复制')
  }
}

async function load() {
  loading.value = true
  try {
    detail.value = await getReview(id)
  } finally {
    loading.value = false
  }
}

async function onFindingStatus(row: ReviewFinding, status: 'waived' | 'active') {
  busy.value = true
  try {
    await setFindingStatus(row.id, status)
    await load()
  } finally {
    busy.value = false
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
.agent-links {
  margin-top: 16px;
  display: flex;
  gap: 12px;
}
.review-toolbar {
  margin-top: 16px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.toolbar-hint {
  font-size: 12px;
  color: #909399;
}
.summary {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
.finding-detail {
  padding: 8px 16px;
  background: #fafafa;
}
.finding-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.finding-meta {
  margin-left: 8px;
  font-size: 12px;
  color: #909399;
}
.code-block {
  margin: 8px 0;
}
.code-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 4px;
}
.code-fix {
  color: #67c23a;
}
.code-text {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #fff;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>