<template>
  <div v-loading="loading">
    <el-page-header @back="$router.back()" :content="$t('reviewDetail.pageTitle', { id })" />

    <div v-if="detail" class="review-toolbar">
      <el-button type="primary" plain :disabled="busy || loading" @click="copyMarkdown">
        <el-icon><DocumentCopy /></el-icon>&nbsp;{{ $t('reviewDetail.copyMarkdown') }}
      </el-button>
      <span class="toolbar-hint">{{ $t('reviewDetail.copyHint') }}</span>
    </div>

    <el-card v-if="detail" class="info-card">
      <el-descriptions :column="3" border>
        <el-descriptions-item :label="$t('reviewDetail.field.title')">{{ detail.event_type === 'push' ? '—' : (detail.pr_title || '—') }}</el-descriptions-item>
        <el-descriptions-item :label="$t('common.id')">{{ detail.id }}</el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.provider')">{{ detail.provider }}</el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.repoId')">{{ detail.repo_id }}</el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.prNumber')">{{ detail.pr_number }}</el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.eventType')">{{ detail.event_type }}</el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.branch')">{{ detail.branch }}</el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.state')">
          <el-tag :type="stateTagType(detail.state)">{{ stateLabel(detail.state) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.mode')">
          <el-tag :type="modeTagType(detail.exec_mode)" size="small">{{ modeLabel(detail.exec_mode) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.score')">{{ detail.score_total }}</el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.diffLines')">{{ $t('reviewDetail.diffLinesValue', { n: detail.diff_lines }) }}</el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.headSha')">
          <code class="sha">{{ detail.head_sha }}</code>
        </el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.baseSha')">
          <code class="sha">{{ detail.base_sha || '—' }}</code>
        </el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.queuedAt')">{{ formatTime(detail.queued_at) }}</el-descriptions-item>
        <el-descriptions-item :label="$t('reviewDetail.field.finishedAt')">
          {{ detail.finished_at ? formatTime(detail.finished_at) : '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="Trace ID">{{ detail.trace_id || '—' }}</el-descriptions-item>
        <el-descriptions-item v-if="detail.web_url" :label="$t('reviewDetail.field.webUrl')">
          <el-link :href="detail.web_url" target="_blank" type="primary">
            {{ detail.event_type === 'push' ? $t('reviewDetail.openCommit') : $t('reviewDetail.openMr') }}
          </el-link>
        </el-descriptions-item>
      </el-descriptions>

      <!-- 失败原因分析 -->
      <div v-if="detail.state === 'failed'" class="analysis analysis-fail">
        <b>{{ $t('reviewDetail.failReason') }}</b>
        <pre class="analysis-text">{{ detail.error || $t('reviewDetail.failUnknown') }}</pre>
      </div>

      <!-- 跳过原因 -->
      <div v-if="detail.state === 'skipped'" class="analysis analysis-skip">
        <b>{{ $t('reviewDetail.skipReason') }}</b>
        <pre class="analysis-text">{{ detail.error || $t('reviewDetail.skipDefault') }}</pre>
      </div>

      <!-- agentic 原始对话 / 前后增量对比入口 -->
      <div v-if="detail.event_type === 'mr'" class="agent-links">
        <el-button type="primary" plain @click="$router.push(`/reviews/${id}/conversation`)">
          <el-icon><ChatDotRound /></el-icon>&nbsp;{{ $t('reviewDetail.rawConversation') }}
        </el-button>
        <!-- 首轮无「上次」可对比，隐藏按钮（避免空页噪音） -->
        <el-button
          v-if="detail.prev_round_id != null"
          type="success"
          plain
          @click="$router.push({ path: '/reviews', query: { tab: 'mr', pr_number: detail.pr_number } })"
        >
          <el-icon><DataAnalysis /></el-icon>&nbsp;{{ $t('reviewDetail.compareLast') }}
        </el-button>
      </div>
    </el-card>

    <!-- push 轨：提交消息（多行）单独展示，不当标题 -->
    <el-card v-if="detail?.event_type === 'push' && detail.push_commits" class="info-card">
      <template #header>{{ $t('reviewDetail.pushCommits') }}</template>
      <pre class="summary summary-commits">{{ detail.push_commits }}</pre>
    </el-card>

    <el-card class="info-card">
      <template #header>{{ $t('reviewDetail.summaryHeader') }}</template>
      <pre class="summary">{{ detail?.summary_md || $t('reviewDetail.summaryEmpty') }}</pre>
    </el-card>

    <el-card>
      <template #header>{{ $t('reviewDetail.findingsHeader', { n: detail?.findings?.length ?? 0 }) }}</template>
      <el-table :data="detail?.findings || []" stripe>
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="finding-detail">
              <div class="finding-actions">
                <span v-if="row.first_seen" class="finding-meta">{{ $t('reviewDetail.firstSeen', { time: formatTime(row.first_seen) }) }}</span>
                <span v-if="row.status === 'resolved' && row.last_seen" class="finding-meta">{{ $t('reviewDetail.resolvedAt', { time: formatTime(row.last_seen) }) }}</span>
              </div>
              <div v-if="row.existing_code" class="code-block">
                <div class="code-label">{{ $t('reviewDetail.existingCode') }}</div>
                <pre class="code-text">{{ row.existing_code }}</pre>
              </div>
              <div v-if="row.suggestion" class="code-block">
                <div class="code-label code-fix">{{ $t('reviewDetail.suggestion') }}</div>
                <pre class="code-text">{{ row.suggestion }}</pre>
              </div>
              <div v-if="row.detail" class="code-block">
                <div class="code-label">{{ $t('reviewDetail.detailAnalysis') }}</div>
                <pre class="code-text">{{ row.detail }}</pre>
              </div>
              <el-empty v-if="!row.existing_code && !row.suggestion" :description="$t('reviewDetail.noCode')" :image-size="40" />
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="severity" :label="$t('reviewDetail.col.severity')" width="100">
          <template #default="{ row }">
            <el-tag :type="severityTag(row.severity)">{{ row.severity }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="category" :label="$t('reviewDetail.col.category')" width="110" />
        <el-table-column prop="file" :label="$t('reviewDetail.col.file')" min-width="150" />
        <el-table-column prop="new_line" :label="$t('reviewDetail.col.line')" width="70" />
        <el-table-column prop="title" :label="$t('reviewDetail.col.analysis')" min-width="220" show-overflow-tooltip />
        <el-table-column prop="source" :label="$t('reviewDetail.col.source')" width="90">
          <template #default="{ row }">
            <el-tag :type="sourceTagType(row.source)" size="small">{{ sourceLabel(row.source) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" :label="$t('reviewDetail.col.status')" width="110">
          <template #default="{ row }">
            <el-tag :type="findingStatusTag(row.status)" size="small">
              {{ statusLabel(row.status) }}
              <span v-if="row.status === 'active' && row.reopened_count > 0">{{ $t('reviewDetail.reopened', { n: row.reopened_count }) }}</span>
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ChatDotRound, DataAnalysis, DocumentCopy } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { getReview, type ReviewDetail, type ReviewFinding } from '../api'
import { formatTime, stateTagType, stateLabel, modeLabel, modeTagType } from '../utils/format'

const route = useRoute()
const { t } = useI18n()
const id = Number(route.params.id)
const detail = ref<ReviewDetail | null>(null)
const loading = ref(false)
const busy = ref(false)

function findingStatusTag(status: string | null): any {
  if (status === 'resolved') return 'success'
  return 'info'
}

function statusLabel(status: string | null): string {
  if (status === 'resolved') return t('enum.findingStatus.resolved')
  return t('enum.findingStatus.active')
}

function severityTag(sev: string): any {
  if (sev === 'critical' || sev === 'high' || sev === 'error') return 'danger'
  if (sev === 'medium' || sev === 'warning') return 'warning'
  return 'info'
}

function sourceLabel(source: string | null): string {
  if (!source) return t('common.unknown')
  if (source === 'llm') return 'LLM'
  if (source.startsWith('static')) return t('reviewDetail.sourceStatic')
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
  L.push(`# ${t('reviewDetail.md.header', { id: d.id })}`)
  L.push('')
  L.push(`## ${t('reviewDetail.md.meta')}`)
  L.push(`- ${t('reviewDetail.md.provider', { v: d.provider || '—' })}`)
  L.push(`- ${t('reviewDetail.md.repoId', { v: d.repo_id || '—' })}`)
  L.push(`- ${t('reviewDetail.md.eventType', { v: d.event_type || '—' })}`)
  if (d.pr_number != null) L.push(`- ${t('reviewDetail.md.prNumber', { v: d.pr_number })}`)
  if (d.pr_title) L.push(`- ${t('reviewDetail.md.title', { v: d.pr_title })}`)
  if (d.branch) L.push(`- ${t('reviewDetail.md.branch', { v: d.branch })}`)
  if (d.head_sha) L.push(`- ${t('reviewDetail.md.headSha', { v: d.head_sha })}`)
  if (d.base_sha) L.push(`- ${t('reviewDetail.md.baseSha', { v: d.base_sha })}`)
  L.push(`- ${t('reviewDetail.md.score', { v: d.score_total ?? '—' })}`)
  if (d.diff_lines != null) L.push(`- ${t('reviewDetail.md.diffLines', { n: d.diff_lines })}`)
  if (d.web_url) L.push(`- ${t('reviewDetail.md.webUrl', { v: d.web_url })}`)
  L.push('')

  L.push(`## ${t('reviewDetail.md.summary')}`)
  L.push(d.summary_md || t('reviewDetail.summaryEmpty'))
  L.push('')

  const findings = d.findings || []
  L.push(`## ${t('reviewDetail.md.findings', { n: findings.length })}`)
  if (findings.length === 0) {
    L.push(t('reviewDetail.md.noFindings'))
  } else {
    findings.forEach((f, i) => {
      const sev = f.severity || 'unknown'
      L.push(`### ${t('reviewDetail.md.findingHeading', {
        i: i + 1,
        sev,
        category: f.category || t('reviewDetail.md.uncategorized'),
        title: f.title,
      })}`)
      L.push('')
      L.push(`- ${t('reviewDetail.md.file', { v: f.file })}`)
      L.push(`- ${t('reviewDetail.md.line', { v: f.new_line ?? '—' })}`)
      L.push(`- ${t('reviewDetail.md.source', { v: f.source || t('common.unknown') })}`)
      L.push(`- ${t('reviewDetail.md.status', { v: statusLabel(f.status) })}`)
      if (f.existing_code) {
        L.push('')
        L.push(`**${t('reviewDetail.md.existingCode')}**`)
        L.push(fence(f.existing_code))
      }
      if (f.suggestion) {
        L.push('')
        L.push(`**${t('reviewDetail.md.suggestion')}**`)
        L.push(fence(f.suggestion))
      }
      if (f.detail) {
        L.push('')
        L.push(`**${t('reviewDetail.md.detail')}**`)
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
    ElMessage.success(t('reviewDetail.copied'))
  } catch (e: any) {
    ElMessage.error(t('reviewDetail.copyFailed'))
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
  border: 1px solid var(--el-color-danger-light-7);
  background: var(--el-color-danger-light-9);
}
.analysis-skip {
  border: 1px solid var(--el-border-color);
  background: var(--el-fill-color);
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
  color: var(--el-text-color-secondary);
}
.summary {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
.finding-detail {
  padding: 8px 16px;
  background: var(--el-fill-color-lighter);
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
  color: var(--el-text-color-secondary);
}
.code-block {
  margin: 8px 0;
}
.code-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}
.code-fix {
  color: var(--el-color-success);
}
.code-text {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-bg-color);
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>