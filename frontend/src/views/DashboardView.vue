<template>
  <div>
    <el-row :gutter="20">
      <el-col :span="6">
        <el-card>
          <div class="stat-label">总数</div>
          <div class="stat-value">{{ stats.total }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <div class="stat-label">成功</div>
          <div class="stat-value" style="color: #67c23a">{{ stats.ok }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <div class="stat-label">失败</div>
          <div class="stat-value" style="color: #f56c6c">{{ stats.failed }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card>
          <div class="stat-label">排队中</div>
          <div class="stat-value" style="color: #e6a23c">{{ stats.queued }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="chart-card">
      <template #header>按 state 分布</template>
      <div ref="chartRef" class="chart"></div>
    </el-card>

    <el-card class="table-card">
      <template #header>最近记录</template>
      <el-table :data="recent" stripe>
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="repo_id" label="仓库 ID" min-width="120" />
        <el-table-column prop="pr_number" label="PR" width="80" />
        <el-table-column prop="score_total" label="评分" width="90" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="stateTagType(row.state)">{{ row.state }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="finished_at" label="完成时间" width="180" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import * as echarts from 'echarts'
import { listReviews, type ReviewItem } from '../api'

const chartRef = ref<HTMLDivElement>()
const reviews = ref<ReviewItem[]>([])
const recent = ref<ReviewItem[]>([])
const stats = ref({ total: 0, ok: 0, failed: 0, queued: 0 })

// 一次拿最近 100 条，前端自行统计与出图。
// M5 将改为后端聚合(KPI 接口)，此处是过渡实现。
function computeStats(items: ReviewItem[]) {
  const s = { total: items.length, ok: 0, failed: 0, queued: 0 }
  for (const it of items) {
    if (it.state === 'reviewed' || it.state === 'success') s.ok++
    else if (it.state === 'failed') s.failed++
    else s.queued++
  }
  return s
}

function stateTagType(state: string): any {
  if (state === 'reviewed' || state === 'success') return 'success'
  if (state === 'failed') return 'danger'
  return 'warning'
}

async function loadDash() {
  const res = await listReviews({ limit: 100 })
  reviews.value = res.items || []
  recent.value = reviews.value.slice(0, 10)
  stats.value = computeStats(reviews.value)
  await nextTick()
  drawChart()
}

// ECharts 柱状图：按 state 分布
function drawChart() {
  if (!chartRef.value) return
  const chart = echarts.init(chartRef.value)
  const countByState: Record<string, number> = {}
  for (const it of reviews.value) {
    countByState[it.state] = (countByState[it.state] || 0) + 1
  }
  const states = Object.keys(countByState)
  chart.setOption({
    tooltip: {},
    xAxis: { type: 'category', data: states },
    yAxis: { type: 'value' },
    series: [
      {
        name: '数量',
        type: 'bar',
        data: states.map((s) => countByState[s]),
        itemStyle: { color: '#409eff' },
      },
    ],
  })
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
.chart-card {
  margin-top: 20px;
}
.chart {
  height: 320px;
}
.table-card {
  margin-top: 20px;
}
</style>