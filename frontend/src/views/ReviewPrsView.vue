<template>
  <div>
    <!-- 按 MR 聚合并行视图：筛选 + 完整分页（服务端分页，条件持久化到 URL） -->
    <el-card class="filter-card">
      <template #header>
        <div class="card-head">
          <span class="card-title">MR 审查进展</span>
          <span class="card-actions">
            <el-button @click="onReset">重置</el-button>
            <el-button type="primary" @click="onFilterChange">查询</el-button>
          </span>
        </div>
      </template>
      <el-form inline class="filter-form" @submit.prevent>
        <el-form-item label="平台">
          <el-select
            v-model="query.provider"
            placeholder="全部平台"
            clearable
            filterable
            allow-create
            default-first-option
            style="width: 130px"
            @change="onFilterChange"
          >
            <el-option v-for="o in providerOptions" :key="o" :label="o" :value="o" />
          </el-select>
        </el-form-item>
        <el-form-item label="MR/PR 号">
          <el-input-number
            v-model="query.pr_number"
            :min="1"
            :controls="false"
            placeholder="精确 MR/PR 号"
            style="width: 120px"
            @change="onFilterChange"
          />
        </el-form-item>
        <el-form-item label="标题关键词">
          <el-input
            v-model="query.q"
            placeholder="匹配 MR/PR 标题"
            clearable
            style="width: 180px"
            @keyup.enter="onFilterChange"
            @clear="onFilterChange"
          />
        </el-form-item>
        <el-form-item label="完成时间">
          <el-date-picker
            v-model="query.dateRange"
            type="daterange"
            range-separator="~"
            start-placeholder="开始"
            end-placeholder="结束"
            value-format="YYYY-MM-DD"
            format="YYYY-MM-DD"
            unlink-panels
            style="width: 260px"
            @change="onFilterChange"
          />
        </el-form-item>
      </el-form>
    </el-card>

    <div v-loading="loading" class="pr-list">
      <div v-if="!items.length && !loading" class="empty-tip">暂无已完成审查的 MR（需 ≥1 轮 completed 的 mr 任务）</div>

      <el-card v-for="pr in items" :key="pr.key" class="pr-card" shadow="never">
        <template #header>
          <div class="pr-head">
            <div class="pr-title">
              <el-tag type="primary" effect="plain">PR/MR #{{ pr.pr_number }}</el-tag>
              <span class="title-text">{{ pr.pr_title || '（无标题）' }}</span>
            </div>
            <div class="pr-meta">
              <el-tag size="small" effect="plain">{{ pr.provider }}</el-tag>
              <span class="branch">{{ pr.branch }}</span>
              <span class="rounds-count">共 {{ pr.rounds_count }} 轮</span>
              <span
                v-if="pr.rounds_count > 1"
                class="conv-text"
                :class="pr.rate_pct >= 80 ? 'good' : 'warn'"
                :title="`收敛率 ${pr.rate_pct}%`"
              >收敛 {{ pr.rate_pct }}%</span>
              <span v-else class="conv-text muted">&mdash;</span>
            </div>
          </div>
        </template>

        <!-- 回合时间线：每轮一个紧凑 pill（横向排布，多轮自动换行） -->
        <div class="timeline">
          <span
            v-for="(r, i) in pr.rounds"
            :key="r.id"
            class="tl-round"
            :class="{ current: i === pr.rounds.length - 1 }"
            :title="`第${i + 1}轮 ${r.head_sha}\n+新增 ${r.delta.new} · 持续 ${r.delta.persisting} · 已解决 ${r.delta.resolved} · 未覆盖 ${r.delta.not_reviewed}`"
          >
            第{{ i + 1 }}轮
            <b class="up">+{{ r.delta.new }}</b>
            <b class="down">-{{ r.delta.resolved }}</b>
          </span>
        </div>

        <!-- 末轮四桶明细：上下堆叠、可折叠（长内容占整行宽，不再窄列换行） -->
        <el-alert
          v-if="lastBucket(pr, 'not_reviewed').length"
          type="warning"
          :closable="false"
          show-icon
          title="存在上轮未覆盖（未验证）的问题"
          description="这些问题上次报过、但本轮未审到对应文件（未变更复用/缺失覆盖集时保守登记）。≠ 已修复，需注意。"
          style="margin: 12px 0"
        />
        <div class="buckets">
          <div v-for="b in buckets" :key="pr.key + ':' + b.key" class="bucket-item">
            <div class="bucket-toggle" @click="toggleBucket(pr, b.key)">
              <el-tag :type="b.tag" size="small">{{ b.label }}</el-tag>
              <span class="count">{{ lastBucket(pr, b.key).length }}</span>
              <span class="caret" :class="{ open: isBucketOpen(pr, b.key) }">▸</span>
            </div>
            <div v-if="isBucketOpen(pr, b.key)" class="bucket-body">
              <FindingBucketTable :rows="lastBucket(pr, b.key)" :empty="b.empty" />
            </div>
          </div>
        </div>
      </el-card>

      <el-pagination
        v-if="total > query.limit"
        class="pager"
        layout="total, prev, pager, next, sizes"
        :total="total"
        :page-size="query.limit"
        :current-page="query.offset / query.limit + 1"
        :page-sizes="[10, 20, 50]"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listReviewPrs, type ReviewPr, type CompareBucketItem } from '../api'
import FindingBucketTable from '../components/FindingBucketTable.vue'

const route = useRoute()
const router = useRouter()

const items = ref<ReviewPr[]>([])
const total = ref(0)
const loading = ref(false)
const providerOptions = ['gitlab', 'github', 'gitee']

// 筛选 + 分页（服务端分页），持久化到 URL query，进详情返回后可恢复。
const query = reactive<{
  provider: string
  pr_number: number | null
  q: string
  dateRange: [string, string] | null
  limit: number
  offset: number
}>({
  provider: '',
  pr_number: null,
  q: '',
  dateRange: null,
  limit: 10,
  offset: 0,
})

// 末轮四桶元数据（标签 / 颜色 / 空态文案），配合下方折叠渲染。
const buckets = [
  { key: 'new', label: '新增', tag: 'danger', empty: '本轮无新增问题' },
  { key: 'persisting', label: '持续存在', tag: 'warning', empty: '无持续存在的问题' },
  { key: 'resolved', label: '已解决', tag: 'success', empty: '本轮已全部修复' },
  { key: 'not_reviewed', label: '上次未覆盖', tag: 'info', empty: '无' },
]
// 展开的桶集合（key=`pr.key:桶名`）；默认只展开每张卡的「新增」。
const openBuckets = ref<Set<string>>(new Set())

function lastBucket(pr: ReviewPr, key: string): CompareBucketItem[] {
  return (pr.last_delta as unknown as Record<string, CompareBucketItem[]>)[key] ?? []
}
function isBucketOpen(pr: ReviewPr, key: string): boolean {
  return openBuckets.value.has(pr.key + ':' + key)
}
function toggleBucket(pr: ReviewPr, key: string) {
  const k = pr.key + ':' + key
  const s = new Set(openBuckets.value)
  if (s.has(k)) s.delete(k)
  else s.add(k)
  openBuckets.value = s
}

// 翻页/过滤均重新请求后端（服务端分页），筛选字段不随翻页丢。
function filterParams(): Record<string, string | number | undefined> {
  const p: Record<string, string | number | undefined> = {}
  if (query.provider) p.provider = query.provider
  if (query.pr_number != null) p.pr_number = query.pr_number
  if (query.q) p.q = query.q
  if (query.dateRange) {
    p.finished_from = query.dateRange[0]
    p.finished_to = query.dateRange[1]
  }
  return p
}

async function load() {
  loading.value = true
  try {
    const page = await listReviewPrs({
      ...filterParams(),
      limit: query.limit,
      offset: query.offset,
    })
    items.value = page.items
    total.value = page.total
    // 默认展开每张卡的「新增」桶
    openBuckets.value = new Set(page.items.map((pr) => pr.key + ':new'))
  } finally {
    loading.value = false
  }
}

function onFilterChange() {
  query.offset = 0
  syncUrl()
  load()
}
function onReset() {
  query.provider = ''
  query.pr_number = null
  query.q = ''
  query.dateRange = null
  onFilterChange()
}
function onPageChange(p: number) {
  query.offset = (p - 1) * query.limit
  syncUrl()
  load()
}
function onSizeChange(s: number) {
  query.limit = s
  query.offset = 0
  syncUrl()
  load()
}

// 把当前筛选/分页写入 URL query（保留 host 页的 tab 等参数），供返回/刷新后恢复。
function syncUrl() {
  const q: Record<string, string | number> = { ...route.query }
  if (query.provider) q.provider = query.provider
  if (query.pr_number != null) q.pr_number = query.pr_number
  if (query.q) q.q = query.q
  if (query.dateRange) {
    q.finished_from = query.dateRange[0]
    q.finished_to = query.dateRange[1]
  }
  if (query.limit !== 10) q.limit = query.limit
  if (query.offset > 0) q.offset = query.offset
  router.replace({ query: q })
}
function initFromRoute() {
  const r = route.query
  query.provider = (r.provider as string) || ''
  query.pr_number = r.pr_number != null ? Number(r.pr_number) : null
  query.q = (r.q as string) || ''
  if (r.finished_from && r.finished_to) {
    query.dateRange = [r.finished_from as string, r.finished_to as string]
  }
  query.limit = r.limit ? Number(r.limit) : 10
  query.offset = r.offset ? Number(r.offset) : 0
}

initFromRoute()
onMounted(load)
</script>

<style scoped>
.filter-card {
  margin-bottom: 14px;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.card-title {
  font-weight: 600;
}
.card-actions {
  display: flex;
  gap: 8px;
}
.pr-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.pr-card :deep(.el-card__body) {
  padding: 12px 16px;
}
.pr-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}
.pr-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.title-text {
  font-weight: 600;
}
.pr-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #909399;
  font-size: 12.5px;
}
.branch {
  color: #606266;
}
.conv-text {
  font-size: 12.5px;
  font-weight: 600;
  margin-left: 4px;
}
.conv-text.good {
  color: #67c23a;
}
.conv-text.warn {
  color: #e6a23c;
}
.conv-text.muted {
  color: #909399;
  font-weight: 400;
}
.timeline {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 0 8px;
  align-items: center;
}
.tl-round {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  font-size: 12px;
  color: #606266;
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 12px;
  white-space: nowrap;
  cursor: default;
}
.tl-round.current {
  background: #ecf5ff;
  border-color: #b3d8ff;
  color: #409eff;
}
.tl-round b {
  font-weight: 600;
}
.tl-round .up {
  color: #f56c6c;
}
.tl-round .down {
  color: #67c23a;
}
.buckets {
  margin-top: 6px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  overflow: hidden;
}
.bucket-item + .bucket-item {
  border-top: 1px solid #f0f2f5;
}
.bucket-toggle {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
  background: #fafafa;
}
.bucket-toggle:hover {
  background: #f0f2f5;
}
.count {
  font-size: 13px;
  color: #606266;
}
.caret {
  margin-left: auto;
  color: #909399;
  font-size: 12px;
  transition: transform 0.15s ease;
}
.caret.open {
  transform: rotate(90deg);
}
.bucket-body {
  padding: 8px 12px;
}
.empty-tip {
  color: #909399;
  text-align: center;
  padding: 40px 0;
}
.pager {
  justify-content: flex-end;
}
</style>