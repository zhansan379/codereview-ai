<template>
  <div>
    <div class="dash-toolbar">
      <el-radio-group v-model="mode" size="small" @change="onModeChange">
        <el-radio-button value="all">全部</el-radio-button>
        <el-radio-button value="agentic">Agent</el-radio-button>
        <el-radio-button value="diff">Diff</el-radio-button>
      </el-radio-group>
      <span class="toolbar-hint">按实际执行模式（exec_mode）分组通用图；agent 专属图不受影响</span>
    </div>

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
        <div class="stat-value" style="color: #f56c6c">{{ stats.open_high }}</div>
      </el-card>
      <el-card>
        <div class="stat-label">未解决严重</div>
        <div class="stat-value" style="color: #e6a23c">{{ stats.open_critical }}</div>
      </el-card>
      <el-card>
        <div class="stat-label">平均对话轮数（agent）</div>
        <div class="stat-value">
          {{ stats.avg_chat_rounds }}<span class="stat-unit">轮</span>
        </div>
      </el-card>
    </div>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="12" :xs="24" :md="12">
        <el-card>
          <template #header>严重级别分布</template>
          <KpiList :rows="severityRows" />
        </el-card>
      </el-col>
      <el-col :span="12" :xs="24" :md="12">
        <el-card>
          <template #header>近 14 天审查趋势</template>
          <div ref="trendRef" class="chart"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="12" :xs="24" :md="12">
        <el-card>
          <template #header>任务状态分布</template>
          <KpiList :rows="stateRows" />
        </el-card>
      </el-col>
      <el-col :span="12" :xs="24" :md="12">
        <el-card>
          <template #header>审查渠道分流</template>
          <KpiList :rows="providerRows" />
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
          <template #header>近 14 天审查耗时趋势</template>
          <div ref="durationRef" class="chart"></div>
        </el-card>
      </el-col>
      <el-col :span="12" :xs="24" :md="12">
        <el-card>
          <template #header>Agent 阶段管线分布</template>
          <div ref="phaseRef" class="chart"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="8" :xs="24" :md="8">
        <el-card>
          <template #header>审查模式分布（agent / diff）</template>
          <KpiList :rows="modeRows" />
        </el-card>
      </el-col>
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
    </el-row>

    <el-card class="table-card">
      <template #header>最近记录</template>
      <ReviewsTable :items="recent" :loading="loading" time-field="finished_at" @detail="goDetail" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { getStats, listReviews, type DashboardStats, type ReviewItem, type PhaseBoxItem } from '../api'
import ReviewsTable from '../components/ReviewsTable.vue'
import KpiList from '../components/KpiList.vue'

const trendRef = ref<HTMLDivElement>()
const scatterRef = ref<HTMLDivElement>()
const tokenRef = ref<HTMLDivElement>()
const costRef = ref<HTMLDivElement>()
const durationRef = ref<HTMLDivElement>()
const phaseRef = ref<HTMLDivElement>()
// 模式分组开关：影响通用图（趋势/耗时/严重级/状态/渠道/token/成本/KPI），agent 专属图恒不受影响
const mode = ref<'all' | 'agentic' | 'diff'>('all')
const router = useRouter()
const recent = ref<ReviewItem[]>([])
const loading = ref(false)
const stats = ref<DashboardStats>({
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
})

// ══ 数字统计行：把只有少数取值/两片的饼图/柱图改成可扫读的数字列表 ══

// percentage hint：少数据时直接给占比，一眼看出权重
function pct(n: number, total: number): string {
  return total > 0 ? `${Math.round((n / total) * 100)}%` : ''
}

const SEVERITY_META: Record<string, { label: string; color: string }> = {
  critical: { label: '严重', color: '#f56c6c' },
  high: { label: '高', color: '#e6a23c' },
  medium: { label: '中', color: '#909399' },
  low: { label: '低', color: '#c0c4cc' },
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
  running: { label: '运行中', color: '#409eff' },
  queued: { label: '排队中', color: '#909399' },
  completed: { label: '已完成', color: '#67c23a' },
  failed: { label: '失败', color: '#f56c6c' },
  skipped: { label: '跳过', color: '#e6a23c' },
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
      color: STATE_META[x.key]?.color ?? '#909399',
      hint: pct(x.count, stateTotal.value),
    })),
)

const providerTotal = computed(() => stats.value.provider_split.reduce((s, x) => s + x.count, 0))
const providerRows = computed(() =>
  stats.value.provider_split.map((x) => ({
    label: x.key,
    value: x.count,
    color: '#409eff',
    hint: pct(x.count, providerTotal.value),
  })),
)

const modeTotal = computed(() => stats.value.tasks_by_mode.reduce((s, x) => s + x.count, 0))
const MODE_META: Record<string, { label: string; color: string }> = {
  agentic: { label: 'Agent 审查', color: '#722ed1' },
  diff: { label: 'Diff 审查', color: '#13c2c2' },
}
const modeRows = computed(() =>
  stats.value.tasks_by_mode.map((x) => ({
    label: MODE_META[x.key]?.label ?? x.key,
    value: x.count,
    color: MODE_META[x.key]?.color ?? '#409eff',
    hint: pct(x.count, modeTotal.value),
  })),
)

function drawTrend() {
  if (!trendRef.value) return
  ensure(trendRef.value).setOption({
    tooltip: { trigger: 'axis', confine: true },
    grid: { containLabel: true, left: 8, right: 16, top: 20, bottom: 8 },
    xAxis: {
      type: 'category',
      data: stats.value.reviews_by_day.map((d) => d.day.slice(5)), // MM-DD
    },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{ type: 'line', smooth: true, data: stats.value.reviews_by_day.map((d) => d.count) }],
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
        `diff 行数: ${p.value[0]}<br/>耗时: ${p.value[1]}s<br/>轮数: ${p.value[2]}<br/>工具调用: ${p.value[3]}`,
    },
    grid: { containLabel: true, left: 8, right: 64, top: 20, bottom: 8 },
    xAxis: {
      type: 'value',
      name: 'diff 行数',
      minInterval: 1,
      nameLocation: 'middle',
      nameGap: 28,
    },
    yAxis: {
      type: 'value',
      name: '耗时 (s)',
      nameLocation: 'middle',
      nameGap: 36,
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
  const items = stats.value.model_usage
  ensure(tokenRef.value).setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, confine: true },
    legend: { bottom: 0 },
    // top 预留 y 轴名('token')空间，left 预留轴名横向宽度：containLabel 不收纳轴名，太小会被左侧裁掉
    grid: { containLabel: true, left: 16, right: 16, top: 40, bottom: 24 },
    xAxis: { type: 'category', data: items.map((m) => m.model) },
    yAxis: { type: 'value', name: 'token' },
    series: [
      {
        name: '输入',
        type: 'bar',
        stack: 'tokens',
        data: items.map((m) => m.prompt_tokens),
        barMaxWidth: 40,
      },
      {
        name: '输出',
        type: 'bar',
        stack: 'tokens',
        data: items.map((m) => m.completion_tokens),
        barMaxWidth: 40,
      },
    ],
  })
}

function drawCost() {
  if (!costRef.value) return
  ensure(costRef.value).setOption({
    tooltip: { trigger: 'axis', confine: true },
    // top 预留 y 轴名('成本')空间，left 预留轴名横向宽度：containLabel 不收纳轴名，太小会被左侧裁掉
    grid: { containLabel: true, left: 8, right: 16, top: 40, bottom: 12 },
    xAxis: {
      type: 'category',
      data: stats.value.cost_by_day.map((d) => d.day.slice(5)),
    },
    yAxis: { type: 'value', name: '成本' },
    series: [
      {
        type: 'line',
        smooth: true,
        areaStyle: { opacity: 0.15 },
        data: stats.value.cost_by_day.map((d) => Number(d.cost.toFixed(4))),
      },
    ],
  })
}

// 复用已有实例（模式开关重绘）而非重复 init（重复 init 会抛「已初始化」）
function ensure(el: HTMLDivElement) {
  return echarts.getInstanceByDom(el) ?? echarts.init(el)
}

// Agent 管线阶段规范顺序；未在列出的兜底phase（如 loop）追加在后
const PHASE_ORDER = ['plan', 'main', 're_location', 'review_filter', 'scoring', 'compress']

function drawDuration() {
  if (!durationRef.value) return
  const items = stats.value.duration_by_day
  ensure(durationRef.value).setOption({
    tooltip: {
      trigger: 'axis',
      formatter: (p: { name: string; value: number }[]) => {
        const rec = items.find((d) => d.day.slice(5) === p[0].name)
        return `${p[0].name}<br/>平均耗时: ${p[0].value}s<br/>任务数: ${rec ? rec.count : 0}`
      },
    },
    grid: { containLabel: true, left: 8, right: 16, top: 36, bottom: 8 },
    xAxis: { type: 'category', data: items.map((d) => d.day.slice(5)) },
    yAxis: { type: 'value', name: '耗时 (s)', nameGap: 10 },
    series: [{ type: 'bar', data: items.map((d) => d.avg_seconds), barMaxWidth: 24 }],
  })
}

function drawPhase() {
  if (!phaseRef.value) return
  const byKey = new Map(stats.value.phase_box.map((p) => [p.key, p]))
  const known = PHASE_ORDER.map((ph) => byKey.get(ph)).filter(Boolean) as PhaseBoxItem[]
  const extra = stats.value.phase_box.filter((p) => !PHASE_ORDER.includes(p.key))
  const boxes = [...known, ...extra]
  // 散点覆盖平均/众数：[x 下标, 取值]，boxplot 数据 [min,q1,median,q3,max]
  const boxData = boxes.map((b) => [b.min, b.q1, b.median, b.q3, b.max])
  const meanPts = boxes.map((b, i) => [i, b.mean])
  const modePts = boxes.map((b, i) => [i, b.mode])
  ensure(phaseRef.value).setOption({
    tooltip: {
      trigger: 'item',
      confine: true,
      formatter: (p: { dataIndex: number; seriesType?: string; value: number[] }) => {
        const b = boxes[p.dataIndex]
        if (!b) return ''
        return `${b.key}<br/>参与 task: ${b.task_count} 个<br/>` +
          `${p.seriesType === 'boxplot' ? `最少: ${p.value[0]}　中位: ${b.median}　最多: ${p.value[4]}<br/>` : ''}` +
          `平均: ${b.mean}　众数: ${b.mode}<br/>Q1: ${b.q1}　Q3: ${b.q3}`
      },
    },
    legend: { bottom: 0 },
    // left 预留 y 轴名('单次审查 LLM 调用数')空间：containLabel 不收纳轴名，太小会被左侧裁掉
    grid: { containLabel: true, left: 16, right: 8, top: 24, bottom: 28 },
    xAxis: {
      type: 'category',
      data: boxes.map((b) => b.key),
      axisLabel: { rotate: 20, interval: 0 },
    },
    yAxis: { type: 'value', minInterval: 1, name: '单次审查 LLM 调用数', nameGap: 10 },
    series: [
      { type: 'boxplot', data: boxData },
      { name: '平均', type: 'scatter', symbol: 'diamond', symbolSize: 8, data: meanPts },
      { name: '众数', type: 'scatter', symbol: 'rect', symbolSize: 8, data: modePts },
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
    const [dash, list] = await Promise.all([
      getStats(mode.value),
      listReviews({ limit: 10 }),
    ])
    stats.value = dash
    recent.value = list.items || []
    await nextTick()
    redrawAll()
  } finally {
    loading.value = false
  }
}

async function onModeChange() {
  loading.value = true
  try {
    stats.value = await getStats(mode.value)
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
</script>

<style scoped>
.dash-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.toolbar-hint {
  color: #909399;
  font-size: 13px;
}
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
  color: #909399;
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
  color: #909399;
  margin-left: 2px;
}
.charts-row {
  margin-top: 20px;
}
.chart {
  height: 300px;
  width: 100%;
  box-sizing: border-box;
}
.chart-sm {
  height: 240px;
  width: 100%;
  box-sizing: border-box;
}
.header-sub {
  font-size: 13px;
  font-weight: 400;
  color: #909399;
  margin-left: 8px;
}
.table-card {
  margin-top: 20px;
}
</style>