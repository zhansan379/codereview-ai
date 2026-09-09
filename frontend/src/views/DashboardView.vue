<template>
  <div>
    <el-row :gutter="20">
      <el-col :span="6">
        <el-card>
          <div class="stat-label">审查任务</div>
          <div class="stat-value">{{ stats.total_tasks }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <div class="stat-label">问题总数</div>
          <div class="stat-value">{{ stats.total_findings }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <div class="stat-label">未解决高危</div>
          <div class="stat-value" style="color: #f56c6c">{{ stats.open_high }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <div class="stat-label">未解决严重</div>
          <div class="stat-value" style="color: #e6a23c">{{ stats.open_critical }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="12">
        <el-card>
          <template #header>严重级别分布</template>
          <div ref="severityRef" class="chart"></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>近 14 天审查趋势</template>
          <div ref="trendRef" class="chart"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="12">
        <el-card>
          <template #header>任务状态分布</template>
          <div ref="stateRef" class="chart chart-sm"></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>审查渠道分流</template>
          <div ref="providerRef" class="chart chart-sm"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="charts-row">
      <el-col :span="8">
        <el-card>
          <template #header>审查模式分布（agent / diff）</template>
          <div ref="modeRef" class="chart chart-sm"></div>
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card>
          <template #header>
            Agent 复杂度 × 成本
            <span class="scatter-sub">
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
import { ref, onMounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { getStats, listReviews, type DashboardStats, type ReviewItem } from '../api'
import ReviewsTable from '../components/ReviewsTable.vue'

const severityRef = ref<HTMLDivElement>()
const trendRef = ref<HTMLDivElement>()
const stateRef = ref<HTMLDivElement>()
const providerRef = ref<HTMLDivElement>()
const modeRef = ref<HTMLDivElement>()
const scatterRef = ref<HTMLDivElement>()
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
  provider_split: [],
  tasks_by_mode: [],
  agent_task_count: 0,
  avg_chat_rounds: 0,
  agent_scatter: [],
})

function drawSeverity() {
  if (!severityRef.value) return
  echarts.init(severityRef.value).setOption({
    tooltip: { trigger: 'item' },
    series: [
      {
        type: 'pie',
        radius: ['40%', '68%'],
        data: stats.value.findings_by_severity.map((s) => ({
          name: s.key,
          value: s.count,
        })),
      },
    ],
  })
}

function drawTrend() {
  if (!trendRef.value) return
  echarts.init(trendRef.value).setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 24, bottom: 24 },
    xAxis: {
      type: 'category',
      data: stats.value.reviews_by_day.map((d) => d.day.slice(5)), // MM-DD
    },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{ type: 'line', smooth: true, data: stats.value.reviews_by_day.map((d) => d.count) }],
  })
}

function drawMode() {
  if (!modeRef.value) return
  echarts.init(modeRef.value).setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [
      {
        type: 'pie',
        radius: ['40%', '68%'],
        center: ['50%', '46%'],
        data: stats.value.tasks_by_mode.map((s) => ({
          name: s.key === 'agentic' ? 'agent' : s.key,
          value: s.count,
        })),
      },
    ],
  })
}

function drawScatter() {
  if (!scatterRef.value) return
  const pts = stats.value.agent_scatter
  const maxCalls = Math.max(1, ...pts.map((p) => p.tool_calls))
  echarts.init(scatterRef.value).setOption({
    tooltip: {
      trigger: 'item',
      formatter: (p: { value: number[] }) =>
        `diff 行数: ${p.value[0]}<br/>耗时: ${p.value[1]}s<br/>轮数: ${p.value[2]}<br/>工具调用: ${p.value[3]}`,
    },
    grid: { left: 56, right: 24, top: 28, bottom: 44 },
    xAxis: { type: 'value', name: 'diff 行数', minInterval: 1 },
    yAxis: { type: 'value', name: '耗时 (s)' },
    visualMap: {
      dimension: 3,
      min: 0,
      max: maxCalls,
      itemHeight: 110,
      text: ['工具调用', '少'],
      seriesIndex: 0,
    },
    series: [
      {
        type: 'scatter',
        data: pts.map((p) => [p.diff_lines, p.duration_s, p.chat_rounds, p.tool_calls]),
        symbolSize: (val: number[]) => 8 + Math.min(24, (val[3] || 0) * 2),
      },
    ],
  })
}

function drawBar(el: HTMLDivElement, items: { key: string; count: number }[]) {
  echarts.init(el).setOption({
    tooltip: {},
    xAxis: { type: 'category', data: items.map((i) => i.key) },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', data: items.map((i) => i.count), barMaxWidth: 24 }],
  })
}

function goDetail(id: number) {
  router.push(`/reviews/${id}`)
}

async function loadDash() {
  loading.value = true
  try {
    const [dash, list] = await Promise.all([
      getStats(),
      listReviews({ limit: 10 }),
    ])
    stats.value = dash
    recent.value = list.items || []
    await nextTick()
    drawSeverity()
    drawTrend()
    if (stateRef.value) drawBar(stateRef.value, dash.tasks_by_state)
    if (providerRef.value) drawBar(providerRef.value, dash.provider_split)
    drawMode()
    drawScatter()
  } finally {
    loading.value = false
  }
}

onMounted(loadDash)
</script>

<style scoped>
.stat-label {
  color: #909399;
  font-size: 13px;
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
  margin-top: 6px;
}
.charts-row {
  margin-top: 20px;
}
.chart {
  height: 300px;
}
.chart-sm {
  height: 240px;
}
.scatter-sub {
  font-size: 13px;
  font-weight: 400;
  color: #909399;
  margin-left: 8px;
}
.table-card {
  margin-top: 20px;
}
</style>