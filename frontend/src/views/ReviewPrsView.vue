<template>
  <div>
    <!-- 按 MR 聚合并行视图：筛选 + 完整分页（服务端分页，条件持久化到 URL） -->
    <el-card class="filter-card">
      <template #header>
        <div class="card-head">
          <span class="card-title">{{ $t('reviewPrs.title') }}</span>
          <span class="card-actions">
            <el-button @click="onReset">{{ $t('common.reset') }}</el-button>
            <el-button type="primary" @click="onFilterChange">{{ $t('reviewPrs.search') }}</el-button>
          </span>
        </div>
      </template>
      <el-form inline class="filter-form" @submit.prevent>
        <el-form-item :label="$t('reviewPrs.provider')">
          <el-select
            v-model="query.provider"
            :placeholder="$t('reviewPrs.allProviders')"
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
        <el-form-item :label="$t('reviewPrs.prNumber')">
          <el-input-number
            v-model="query.pr_number"
            :min="1"
            :controls="false"
            :placeholder="$t('reviewPrs.prNumberPlaceholder')"
            style="width: 120px"
            @change="onFilterChange"
          />
        </el-form-item>
        <el-form-item :label="$t('reviewPrs.keyword')">
          <el-input
            v-model="query.q"
            :placeholder="$t('reviewPrs.keywordPlaceholder')"
            clearable
            style="width: 180px"
            @keyup.enter="onFilterChange"
            @clear="onFilterChange"
          />
        </el-form-item>
        <el-form-item :label="$t('reviewPrs.finishedAt')">
          <el-date-picker
            v-model="query.dateRange"
            type="daterange"
            range-separator="~"
            :start-placeholder="$t('reviewPrs.dateStart')"
            :end-placeholder="$t('reviewPrs.dateEnd')"
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
      <div v-if="!items.length && !loading" class="empty-tip">{{ $t('reviewPrs.empty') }}</div>

      <el-card v-for="pr in items" :key="pr.key" class="pr-card" shadow="never">
        <template #header>
          <div class="pr-head">
            <div class="pr-title">
              <el-tag type="primary" effect="plain">PR/MR #{{ pr.pr_number }}</el-tag>
              <span class="title-text">{{ pr.pr_title || $t('reviewPrs.noTitle') }}</span>
            </div>
            <div class="pr-meta">
              <el-tag size="small" effect="plain">{{ pr.provider }}</el-tag>
              <span class="branch">{{ pr.branch }}</span>
              <span class="rounds-count">{{ $t('reviewPrs.roundsCount', { n: pr.rounds_count }) }}</span>
              <span
                v-if="pr.rounds_count > 1"
                class="conv-text"
                :class="pr.rate_pct >= 80 ? 'good' : 'warn'"
                :title="$t('reviewPrs.convergenceTitle', { pct: pr.rate_pct })"
              >{{ $t('reviewPrs.convergence', { pct: pr.rate_pct }) }}</span>
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
            :title="roundTitle(r, i)"
          >
            {{ $t('reviewPrs.round', { n: i + 1 }) }}
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
          :title="$t('reviewPrs.notReviewedAlertTitle')"
          :description="$t('reviewPrs.notReviewedAlertDesc')"
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
import { ref, reactive, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { listReviewPrs, type ReviewPr, type CompareBucketItem } from '../api'
import FindingBucketTable from '../components/FindingBucketTable.vue'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

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
// computed 而非常量：桶标签与空态提示要随语言切换。
const buckets = computed(() =>
  (
    [
      { key: 'new', tag: 'danger' },
      { key: 'persisting', tag: 'warning' },
      { key: 'resolved', tag: 'success' },
      { key: 'not_reviewed', tag: 'info' },
    ] as const
  ).map((b) => ({
    ...b,
    label: t(`enum.bucket.${b.key}`),
    empty: t(`reviewPrs.emptyBucket.${b.key}`),
  })),
)

/** 回合 pill 的悬浮提示：轮次 + head sha + 四桶差量。 */
function roundTitle(r: ReviewPr['rounds'][number], i: number): string {
  return t('reviewPrs.roundTitle', {
    n: i + 1,
    sha: r.head_sha,
    new: r.delta.new,
    persisting: r.delta.persisting,
    resolved: r.delta.resolved,
    notReviewed: r.delta.not_reviewed,
  })
}
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
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
}
.branch {
  color: var(--el-text-color-regular);
}
.conv-text {
  font-size: 12.5px;
  font-weight: 600;
  margin-left: 4px;
}
.conv-text.good {
  color: var(--el-color-success);
}
.conv-text.warn {
  color: var(--el-color-warning);
}
.conv-text.muted {
  color: var(--el-text-color-secondary);
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
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-light);
  border-radius: 12px;
  white-space: nowrap;
  cursor: default;
}
.tl-round.current {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-5);
  color: var(--el-color-primary);
}
.tl-round b {
  font-weight: 600;
}
.tl-round .up {
  color: var(--el-color-danger);
}
.tl-round .down {
  color: var(--el-color-success);
}
.buckets {
  margin-top: 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  overflow: hidden;
}
.bucket-item + .bucket-item {
  border-top: 1px solid var(--el-border-color-lighter);
}
.bucket-toggle {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
  background: var(--el-fill-color-lighter);
}
.bucket-toggle:hover {
  background: var(--el-bg-color-page);
}
.count {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.caret {
  margin-left: auto;
  color: var(--el-text-color-secondary);
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
  color: var(--el-text-color-secondary);
  text-align: center;
  padding: 40px 0;
}
.pager {
  justify-content: flex-end;
}
</style>