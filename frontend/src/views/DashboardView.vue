<template>
  <div>
    <div class="kpi-grid">
      <el-card>
        <div class="stat-label">审查任务</div>
        <div class="stat-value">{{ stats.total_tasks }}</div>
      </el-card>
      <el-card>
        <div class="stat-label">问题总数</div>
        <div class="stat-value">{{ stats.total_findings }}</div>
      </el-card>
      <el-card>
        <div class="stat-label">未解决高危</div>
        <div class="stat-value" style="color: var(--el-color-danger)">{{ stats.open_high }}</div>
      </el-card>
      <el-card>
        <div class="stat-label">未解决严重</div>
        <div class="stat-value" style="color: var(--el-color-warning)">{{ stats.open_critical }}</div>
      </el-card>
      <el-card>
        <div class="stat-label">平均对话轮数（agent）</div>
        <div class="stat-value">
          {{ stats.avg_chat_rounds }}<span class="stat-unit">轮</span>
        </div>
      </el-card>
    </div>

    <!-- 分布概览：4 个数字分布等宽排一行，瓦片撑满卡片，避免短列表留白 -->
    <el-row :gutter="20" class="charts-row band-row">
      <el-col :xs="12" :md="6">
        <el-card class="band-card">
          <template #header>严重级别分布</template>
          <KpiList :rows="severityRows" />
        </el-card>
      </el-col>
      <el-col :xs="12" :md="6">
        <el-card class="band-card">
          <template #header>任务状态分布</template>
          <KpiList :rows="stateRows" />
        </el-card>
      </el-col>
      <el-col :xs="12" :md="6">
        <el-card class="band-card">
          <template #header>审查渠道分流</template>
          <KpiList :rows="providerRows" />
        </el-card>
      </el-col>
      <el-col :xs="12" :md="6">
        <el-card class="band-card">
          <template #header>审查模式（agent / diff）</template>
          <KpiList :rows="modeRows" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="12" :xs="24" :md="12">
        <el-card>
          <template #header>近 14 天审查趋势</template>
          <div ref="trendRef" class="chart"></div>
        </el-card>
      </el-col>
      <el-col :span="12" :xs="24" :md="12">
        <el-card>
          <template #header>
            近 14 天成本
            <span class="header-sub">
              累计 {{ totalCost }}
            </span>
          </template>
          <div ref="costRef" class="chart"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="12" :xs="24" :md="12">
        <el-card>
          <template #header>模型 Token 用量</template>
          <div ref="tokenRef" class="chart"></div>
        </el-card>
      </el-col>
      <el-col :span="12" :xs="24" :md="12">
        <el-card>
          <template #header>近 14 天审查耗时趋势</template>
          <div ref="durationRef" class="chart"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="16" :xs="24" :md="16">
        <el-card>
          <template #header>
            Agent 复杂度 × 成本
            <span class="header-sub">
              {{ stats.agent_task_count }} 次 agent 审查 · 平均 {{ stats.avg_chat_rounds }} 轮
            </span>
          </template>
          <div ref="scatterRef" class="chart"></div>
        </el-card>
      </el-col>
      <el-col :span="8" :xs="24" :md="8">
        <el-card>
          <template #header>Agent 阶段管线分布</template>
          <div ref="phaseRef" class="chart"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="table-card">
      <template #header>最近记录</template>
      <ReviewsTable :items="recent" :loading="loading" time-field="finished_at" @detail="goDetail" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { getStats, listReviews, type DashboardStats, type ReviewItem, type PhaseBoxItem } from '../api'
import ReviewsTable from '../components/ReviewsTable.vue'
import KpiList from '../components/KpiList.vue'
import { useDark } from '../composables/useDark'

const { isDark } = useDark()
// 记住每个容器当前用的 echarts 主题，切换时才 dispose 重建
const chartTheme = new WeakMap<HTMLDivElement, string | undefined>()

const trendRef = ref<HTMLDivElement>()
const scatterRef = ref<HTMLDivElement>()
const tokenRef = ref<HTMLDivElement>()
const costRef = ref<HTMLDivElement>()
const durationRef = ref<HTMLDivElement>()
const phaseRef = ref<HTMLDivElement>()
const router = useRouter()
const recent = ref<ReviewItem[]>([])
const loading = ref(false)

function emptyStats(): DashboardStats {
  return {
    total_tasks: 0,
    total_findings: 0,
    open_critical: 0,
    open_high: 0,
    tasks_by_state: [],
    findings_by_severity: [],
    findings_by_category: [],
    reviews_by_day: [],
    model_usage: [],
    cost_by_day: [],
    duration_by_day: [],
    phase_box: [],
    provider_split: [],
    tasks_by_mode: [],
    agent_task_count: 0,
    avg_chat_rounds: 0,
    agent_scatter: [],
  }
}

// 账户层：`all` 供 KPI/分布带/专属图；agentic / diff 供账本图内嵌双序列（不靠顶部开关）
const stats = ref<DashboardStats>(emptyStats())
const agentStats = ref<DashboardStats>(emptyStats())
const diffStats = ref<DashboardStats>(emptyStats())
const AGENT_COLOR = '#722ed1'
const DIFF_COLOR = '#13c2c2'

// ══ 数字统计行：把只有少数取值/两片的饼图/柱图改成可扫读的数字列表 ══

// percentage hint：少数据时直接给占比，一眼看出权重
function pct(n: number, total: number): string {
  return total > 0 ? `${Math.round((n / total) * 100)}%` : ''
}

// 秒 → 「X 分 Y 秒」，不足 1 分钟只显示秒，整分钟省略秒
function fmtDur(sec: number): string {
  const s = Math.round(sec)
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  const r = s % 60
  return r ? `${m} 分 ${r} 秒` : `${m} 分`
}

const SEVERITY_META: Record<string, { label: string; color: string }> = {
  critical: { label: '严重', color: 'var(--el-color-danger)' },
  high: { label: '高', color: 'var(--el-color-warning)' },
  medium: { label: '中', color: 'var(--el-text-color-secondary)' },
  low: { label: '低', color: 'var(--el-text-color-placeholder)' },
}

const severityTotal = computed(() => stats.value.findings_by_severity.reduce((s, x) => s + x.count, 0))
const severityRows = computed(() =>
  stats.value.findings_by_severity.map((x) => ({
    label: SEVERITY_META[x.key]?.label ?? x.key,
    value: x.count,
    color: SEVERITY_META[x.key]?.color,
    hint: pct(x.count, severityTotal.value),
  })),
)

const STATE_META: Record<string, { label: string; color: string }> = {
  running: { label: '运行中', color: 'var(--el-color-primary)' },
  queued: { label: '排队中', color: 'var(--el-text-color-secondary)' },
  completed: { label: '已完成', color: 'var(--el-color-success)' },
  failed: { label: '失败', color: 'var(--el-color-danger)' },
  skipped: { label: '跳过', color: 'var(--el-color-warning)' },
}
const STATE_ORDER = ['running', 'queued', 'completed', 'failed', 'skipped']

const stateTotal = computed(() => stats.value.tasks_by_state.reduce((s, x) => s + x.count, 0))
const stateRows = computed(() =>
  // 已知状态按 STATE_ORDER 排，未知状态（如 pending）追加在后
  [...STATE_ORDER, ...stats.value.tasks_by_state.filter((x) => !STATE_ORDER.includes(x.key)).map((x) => x.key)]
    .map((key) => stats.value.tasks_by_state.find((x) => x.key === key))
    .filter((x): x is { key: string; count: number } => !!x)
    .map((x) => ({
      label: STATE_META[x.key]?.label ?? x.key,
      value: x.count,
      color: STATE_META[x.key]?.color ?? 'var(--el-text-color-secondary)',
      hint: pct(x.count, stateTotal.value),
    })),
)

const providerTotal = computed(() => stats.value.provider_split.reduce((s, x) => s + x.count, 0))
const providerRows = computed(() =>
  stats.value.provider_split.map((x) => ({
    label: x.key,
    value: x.count,
    color: 'var(--el-color-primary)',
    hint: pct(x.count, providerTotal.value),
  })),
)

const modeTotal = computed(() => stats.value.tasks_by_mode.reduce((s, x) => s + x.count, 0))
const MODE_META: Record<string, { label: string; color: string }> = {
  agentic: { label: 'Agent 审查', color: AGENT_COLOR },
  diff: { label: 'Diff 审查', color: DIFF_COLOR },
}
const modeRows = computed(() =>
  stats.value.tasks_by_mode.map((x) => ({
    label: MODE_META[x.key]?.label ?? x.key,
    value: x.count,
    color: MODE_META[x.key]?.color ?? 'var(--el-color-primary)',
    hint: pct(x.count, modeTotal.value),
  })),
)

function drawTrend() {
  if (!trendRef.value) return
  ensure(trendRef.value).setOption({
    tooltip: { trigger: 'axis', confine: true },
    legend: { bottom: 0 },
    // left 预留 y 轴名('审查数')空间：containLabel 不收纳轴名，太小会被左侧裁掉
    grid: { containLabel: true, left: 16, right: 16, top: 28, bottom: 24 },
    xAxis: {
      type: 'category',
      data: stats.value.reviews_by_day.map((d) => d.day.slice(5)), // MM-DD
    },
    yAxis: { type: 'value', minInterval: 1, name: '审查数', nameGap: 10 },
    series: [
      {
        name: 'Agent',
        type: 'line',
        smooth: true,
        itemStyle: { color: AGENT_COLOR },
        lineStyle: { color: AGENT_COLOR },
        data: agentStats.value.reviews_by_day.map((d) => d.count),
      },
      {
        name: 'Diff',
        type: 'line',
        smooth: true,
        itemStyle: { color: DIFF_COLOR },
        lineStyle: { color: DIFF_COLOR },
        data: diffStats.value.reviews_by_day.map((d) => d.count),
      },
    ],
  })
}

function drawScatter() {
  if (!scatterRef.value) return
  const pts = stats.value.agent_scatter
  const maxCalls = Math.max(1, ...pts.map((p) => p.tool_calls))
  // containLabel+confine 把轴名/刻度/tooltip 全圈进卡片内，右侧给 visualMap 色带预留空间，避免内容溢出
  ensure(scatterRef.value).setOption({
    tooltip: {
      trigger: 'item',
      confine: true,
      formatter: (p: { value: number[] }) =>
        `diff 行数: ${p.value[0]}<br/>耗时: ${fmtDur(p.value[1])}<br/>轮数: ${p.value[2]}<br/>工具调用: ${p.value[3]}`,
    },
    grid: { containLabel: true, left: 8, right: 64, top: 28, bottom: 8 },
    xAxis: {
      type: 'value',
      name: 'diff 行数',
      minInterval: 1,
      nameLocation: 'end',
      nameGap: 12,
    },
    yAxis: {
      type: 'value',
      name: '耗时',
      nameLocation: 'end',
      nameGap: 8,
      axisLabel: { formatter: (v: number) => fmtDur(v) },
    },
    visualMap: {
      dimension: 3,
      min: 0,
      max: maxCalls,
      itemWidth: 14,
      itemHeight: 110,
      right: 0,
      top: 'center',
      text: ['工具调用', '少'],
      seriesIndex: 0,
    },
    series: [
      {
        type: 'scatter',
        data: pts.map((p) => [p.diff_lines, p.duration_s, p.chat_rounds, p.tool_calls]),
        symbolSize: (val: number[]) => 6 + Math.min(16, (val[3] || 0) * 1.5),
      },
    ],
  })
}

const totalCost = computed(() =>
  stats.value.cost_by_day.reduce((s, d) => s + d.cost, 0).toFixed(2)
)

function drawToken() {
  if (!tokenRef.value) return
  // 模型并集（agent/diff 各自可能用小/大模型），缺失侧填 0
  const models = [...new Set([
    ...agentStats.value.model_usage.map((m) => m.model),
    ...diffStats.value.model_usage.map((m) => m.model),
  ])]
  const aMap = new Map(agentStats.value.model_usage.map((m) => [m.model, m]))
  const dMap = new Map(diffStats.value.model_usage.map((m) => [m.model, m]))
  const pick = (map: Map<string, { prompt_tokens: number; completion_tokens: number }>, f: 'prompt_tokens' | 'completion_tokens') =>
    models.map((m) => (map.get(m) ? map.get(m)![f] : 0))
  ensure(tokenRef.value).setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, confine: true },
    legend: { bottom: 0 },
    // top 预留 y 轴名('token')空间，left 预留轴名横向宽度：containLabel 不收纳轴名，太小会被左侧裁掉
    grid: { containLabel: true, left: 16, right: 16, top: 40, bottom: 24 },
    xAxis: { type: 'category', data: models },
    yAxis: { type: 'value', name: 'token' },
    series: [
      { name: 'Agent 输入', type: 'bar', stack: 'agent', barMaxWidth: 22, itemStyle: { color: AGENT_COLOR }, data: pick(aMap, 'prompt_tokens') },
      { name: 'Agent 输出', type: 'bar', stack: 'agent', barMaxWidth: 22, itemStyle: { color: AGENT_COLOR, opacity: 0.4 }, data: pick(aMap, 'completion_tokens') },
      { name: 'Diff 输入', type: 'bar', stack: 'diff', barMaxWidth: 22, itemStyle: { color: DIFF_COLOR }, data: pick(dMap, 'prompt_tokens') },
      { name: 'Diff 输出', type: 'bar', stack: 'diff', barMaxWidth: 22, itemStyle: { color: DIFF_COLOR, opacity: 0.4 }, data: pick(dMap, 'completion_tokens') },
    ],
  })
}

function drawCost() {
  if (!costRef.value) return
  ensure(costRef.value).setOption({
    tooltip: { trigger: 'axis', confine: true },
    legend: { bottom: 0 },
    // top 预留 y 轴名('成本')空间，left 预留轴名横向宽度：containLabel 不收纳轴名，太小会被左侧裁掉
    grid: { containLabel: true, left: 8, right: 16, top: 40, bottom: 24 },
    xAxis: {
      type: 'category',
      data: stats.value.cost_by_day.map((d) => d.day.slice(5)),
    },
    yAxis: { type: 'value', name: '成本' },
    series: [
      {
        name: 'Agent',
        type: 'line',
        smooth: true,
        itemStyle: { color: AGENT_COLOR },
        lineStyle: { color: AGENT_COLOR },
        data: agentStats.value.cost_by_day.map((d) => Number(d.cost.toFixed(4))),
      },
      {
        name: 'Diff',
        type: 'line',
        smooth: true,
        itemStyle: { color: DIFF_COLOR },
        lineStyle: { color: DIFF_COLOR },
        data: diffStats.value.cost_by_day.map((d) => Number(d.cost.toFixed(4))),
      },
    ],
  })
}

// 复用已有实例（模式开关重绘）而非重复 init（重复 init 会抛「已初始化」）；
// 但亮/暗切换必须换 echarts 主题，而主题只能在 init 时定——所以主题变了就先 dispose。
function ensure(el: HTMLDivElement) {
  const theme = isDark.value ? 'dark' : undefined
  const existing = echarts.getInstanceByDom(el)
  if (existing) {
    if (chartTheme.get(el) === theme) return existing
    existing.dispose()
  }
  chartTheme.set(el, theme)
  const inst = echarts.init(el, theme)
  // echarts 内置 dark 主题自带深紫底色，这里让卡片背景透出来
  inst.setOption({ backgroundColor: 'transparent' })
  return inst
}

// Agent 管线阶段规范顺序；未在列出的兜底phase（如 loop）追加在后
const PHASE_ORDER = ['plan', 'main', 're_location', 'review_filter', 'scoring', 'compress']

function drawDuration() {
  if (!durationRef.value) return
  ensure(durationRef.value).setOption({
    tooltip: {
      trigger: 'axis',
      confine: true,
      formatter: (p: { name: string; marker: string; seriesName: string; value: number }[]) =>
        `${p[0].name}<br/>` +
        p.map((x) => `${x.marker}${x.seriesName}　${fmtDur(x.value)}`).join('<br/>'),
    },
    legend: { bottom: 0 },
    grid: { containLabel: true, left: 8, right: 16, top: 36, bottom: 24 },
    xAxis: { type: 'category', data: stats.value.duration_by_day.map((d) => d.day.slice(5)) },
    yAxis: {
      type: 'value',
      name: '耗时',
      nameGap: 10,
      axisLabel: { formatter: (v: number) => fmtDur(v) },
    },
    series: [
      {
        name: 'Agent',
        type: 'bar',
        barMaxWidth: 14,
        itemStyle: { color: AGENT_COLOR },
        data: agentStats.value.duration_by_day.map((d) => d.avg_seconds),
      },
      {
        name: 'Diff',
        type: 'bar',
        barMaxWidth: 14,
        itemStyle: { color: DIFF_COLOR },
        data: diffStats.value.duration_by_day.map((d) => d.avg_seconds),
      },
    ],
  })
}

function drawPhase() {
  if (!phaseRef.value) return
  const byKey = new Map(stats.value.phase_box.map((p) => [p.key, p]))
  const known = PHASE_ORDER.map((ph) => byKey.get(ph)).filter(Boolean) as PhaseBoxItem[]
  const extra = stats.value.phase_box.filter((p) => !PHASE_ORDER.includes(p.key))
  const boxes = [...known, ...extra]
  // 雷达图：每个 phase 一根轴，四序列（最少/众数/平均/最多）叠成多边形，跨阶段对比。
  // 每轴以该 phase 观测到的 max 为满标 → 各阶段在同一规格下比相对轮廓，避免低值阶段被压扁
  const indicators = boxes.map((b) => ({ name: b.key, max: b.max }))
  const pick = (f: (b: PhaseBoxItem) => number) => boxes.map(f)
  ensure(phaseRef.value).setOption({
    tooltip: {
      trigger: 'item',
      confine: true,
      // 雷达悬停命中的是某个「度量」的整根多边形（params.value 按月份顺序排布），
      // 不暴露具体月份索引 → 逐月列出该度量的调用次数，比单点更具体可读
      formatter: (p: { name: string; value: number | number[] }) => {
        const arr = Array.isArray(p.value) ? p.value : []
        const fmt = (v: number) =>
          Number.isInteger(v) ? `${v}` : `${Math.round(v * 100) / 100}`
        const lines = boxes.map((b, i) =>
          `${b.key}　${arr[i] == null ? '—' : `${fmt(arr[i])} 次`}`
        )
        return `<b>${p.name}</b>（各阶段单次审查 LLM 调用次数）<br/>${lines.join('<br/>')}`
      },
    },
    legend: { bottom: 0 },
    radar: {
      indicator: indicators,
      radius: '62%',
      splitNumber: 4,
      name: { textStyle: { fontSize: 11 } },
    },
    series: [
      {
        type: 'radar',
        data: [
          { name: '最少', value: pick((b) => b.min) },
          { name: '众数', value: pick((b) => b.mode) },
          { name: '平均', value: pick((b) => b.mean) },
          { name: '最多', value: pick((b) => b.max) },
        ],
      },
    ],
  })
}

function goDetail(id: number) {
  router.push(`/reviews/${id}`)
}

// 响应式下窗口变化时让已初始化的图表实例 reflow 到新尺寸（而非被裁剪）
const chartEls = [trendRef, scatterRef, tokenRef, costRef, durationRef, phaseRef]
function resizeCharts() {
  chartEls.forEach((r) => {
    const el = r.value
    if (el) echarts.getInstanceByDom(el)?.resize()
  })
}

function redrawAll() {
  drawTrend()
  drawScatter()
  drawToken()
  drawCost()
  drawDuration()
  drawPhase()
}

async function loadDash() {
  loading.value = true
  try {
    const [all, agent, diff, list] = await Promise.all([
      getStats('all'),
      getStats('agentic'),
      getStats('diff'),
      listReviews({ limit: 10 }),
    ])
    stats.value = all
    agentStats.value = agent
    diffStats.value = diff
    recent.value = list.items || []
    await nextTick()
    redrawAll()
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  window.addEventListener('resize', resizeCharts)
  loadDash()
})
onUnmounted(() => window.removeEventListener('resize', resizeCharts))

// 暗色切换后按新主题重建图表（轴文字/网格线颜色跟着走）
watch(isDark, () => redrawAll())
</script>

<style scoped>
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 16px;
}
@media (max-width: 992px) {
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
.stat-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
  margin-top: 6px;
}
.stat-unit {
  font-size: 14px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
  margin-left: 2px;
}
.charts-row {
  margin-top: 20px;
}
/* 分布概览带：卡片等高、瓦片撑满，消除短列表导致的底部空洞 */
.band-row .band-card {
  height: 100%;
}
.band-row .band-card :deep(.el-card__body) {
  height: 100%;
  padding: 16px 20px;
}
.chart {
  height: 300px;
  width: 100%;
  box-sizing: border-box;
}
.header-sub {
  font-size: 13px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
  margin-left: 8px;
}
.table-card {
  margin-top: 20px;
}
</style>