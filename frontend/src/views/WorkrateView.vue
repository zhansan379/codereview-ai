<template>
  <div v-loading="loading">
    <!-- 筛选：项目 / 用户 / 时间范围（任一变化即重新拉取） -->
    <el-card class="filter-card">
      <div class="filter-row">
        <span class="filter-label">{{ $t('workrate.filters.project') }}</span>
        <el-select
          v-model="projectId"
          clearable
          :placeholder="$t('workrate.filters.allProjects')"
          class="filter-item"
        >
          <el-option
            v-for="p in projectOptions"
            :key="p.id"
            :label="`${p.name} (${p.count})`"
            :value="p.id!"
          />
        </el-select>
        <span class="filter-label">{{ $t('workrate.filters.author') }}</span>
        <el-select
          v-model="author"
          clearable
          filterable
          :placeholder="$t('workrate.filters.allAuthors')"
          class="filter-item"
        >
          <el-option
            v-for="a in optionData.authors"
            :key="a.name"
            :label="`${a.name} (${a.count})`"
            :value="a.name"
          />
        </el-select>
        <span class="filter-label">{{ $t('workrate.filters.days') }}</span>
        <el-select v-model="days" class="filter-item filter-days">
          <el-option :label="$t('workrate.filters.d30')" :value="30" />
          <el-option :label="$t('workrate.filters.d60')" :value="60" />
          <el-option :label="$t('workrate.filters.d90')" :value="90" />
          <el-option :label="$t('workrate.filters.d180')" :value="180" />
          <el-option :label="$t('workrate.filters.dAll')" :value="0" />
        </el-select>
      </div>
      <div v-if="report.scope.commits > 0" class="scope-info">
        {{
          $t('workrate.scopeInfo', {
            from: report.scope.from,
            to: report.scope.to,
            author: report.scope.author || $t('workrate.scopeAll'),
            projects: report.scope.projects_count,
            commits: report.scope.commits,
          })
        }}
      </div>
    </el-card>

    <el-empty v-if="report.scope.commits === 0 && !loading" :description="$t('workrate.empty')" />

    <template v-else>
      <!-- 关键指标卡片 -->
      <div class="kpi-grid">
        <el-card>
          <div class="stat-label">{{ $t('workrate.kpi.total') }}</div>
          <div class="stat-value">{{ report.kpi.total }}<span class="stat-unit">{{ $t('workrate.unit.times') }}</span></div>
        </el-card>
        <el-card>
          <div class="stat-label">{{ $t('workrate.kpi.activeDays') }}</div>
          <div class="stat-value">{{ report.kpi.active_days }}<span class="stat-unit">{{ $t('workrate.unit.days') }}</span></div>
        </el-card>
        <el-card>
          <div class="stat-label">{{ $t('workrate.kpi.dailyAvg') }}</div>
          <div class="stat-value">{{ report.kpi.daily_avg }}<span class="stat-unit">{{ $t('workrate.unit.perDay') }}</span></div>
        </el-card>
        <el-card>
          <div class="stat-label">{{ $t('workrate.kpi.streak') }}</div>
          <div class="stat-value">{{ report.kpi.longest_streak }}<span class="stat-unit">{{ $t('workrate.unit.days') }}</span></div>
          <div class="stat-sub">{{ report.kpi.streak_from }} ~ {{ report.kpi.streak_to }}</div>
        </el-card>
        <el-card>
          <div class="stat-label">{{ $t('workrate.kpi.lateNight') }}</div>
          <div class="stat-value danger-text">
            {{ report.kpi.late_night }}<span class="stat-unit">{{ $t('workrate.unit.times') }}</span>
          </div>
          <div class="stat-sub">{{ report.kpi.late_night_pct }}%</div>
        </el-card>
        <el-card>
          <div class="stat-label">{{ $t('workrate.kpi.weekend') }}</div>
          <div class="stat-value warning-text">
            {{ report.kpi.weekend }}<span class="stat-unit">{{ $t('workrate.unit.times') }}</span>
          </div>
          <div class="stat-sub">{{ report.kpi.weekend_pct }}%</div>
        </el-card>
        <el-card>
          <div class="stat-label">{{ $t('workrate.kpi.nightOrWeekend') }}</div>
          <div class="stat-value warning-text">
            {{ report.kpi.night_or_weekend }}<span class="stat-unit">{{ $t('workrate.unit.times') }}</span>
          </div>
          <div class="stat-sub">{{ report.kpi.night_or_weekend_pct }}%</div>
        </el-card>
      </div>

      <!-- 工作辛苦指数：总分 + 六维进度条 -->
      <el-card class="section">
        <template #header>
          {{ $t('workrate.index.title') }}
          <el-tag :type="levelTag" class="level-tag">{{ levelText }}</el-tag>
        </template>
        <div class="index-row">
          <div class="index-score" :class="`level-${report.index.level}`">
            {{ report.index.score.toFixed(1) }}
          </div>
          <div class="index-desc">{{ $t(`workrate.index.desc.${report.index.level}`) }}</div>
        </div>
        <div class="part-list">
          <div v-for="p in report.index.parts" :key="p.code" class="part-row">
            <span class="part-label">{{ $t(`workrate.index.parts.${p.code}`) }}</span>
            <el-progress
              class="part-bar"
              :percentage="partPct(p)"
              :stroke-width="10"
              :show-text="false"
              :color="levelColor"
            />
            <span class="part-score">{{
              $t('workrate.index.partHint', { value: p.score, max: p.max })
            }}</span>
          </div>
        </div>
      </el-card>

      <!-- 24 小时分布（横条，按时段带着色） -->
      <el-card class="section">
        <template #header>
          {{ $t('workrate.charts.hourly') }}
          <span class="legend">
            <span v-for="b in shownBands" :key="b" class="legend-item">
              <i class="legend-dot" :style="{ background: BAND_COLORS[b] }" />
              {{ $t(`workrate.band.${b}`) }}
            </span>
          </span>
        </template>
        <div ref="hourlyRef" class="chart chart-wide"></div>
      </el-card>

      <el-row :gutter="20" class="section">
        <el-col :span="12" :xs="24">
          <el-card>
            <template #header>{{ $t('workrate.charts.weekday') }}</template>
            <div ref="weekdayRef" class="chart"></div>
          </el-card>
        </el-col>
        <el-col :span="12" :xs="24">
          <el-card>
            <template #header>{{ $t('workrate.charts.monthly') }}</template>
            <div ref="monthlyRef" class="chart"></div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="20" class="section">
        <!-- 用户排行：每个用户的提交概览与辛苦指数 -->
        <el-col :span="14" :xs="24">
          <el-card>
            <template #header>{{ $t('workrate.authors.title') }}</template>
            <el-table :data="report.authors" size="small">
              <el-table-column prop="name" :label="$t('workrate.authors.user')" min-width="100" />
              <el-table-column prop="commits" :label="$t('workrate.authors.commits')" width="80" />
              <el-table-column prop="active_days" :label="$t('workrate.authors.activeDays')" width="90" />
              <el-table-column prop="late_night" :label="$t('workrate.authors.lateNight')" width="70" />
              <el-table-column prop="weekend" :label="$t('workrate.authors.weekend')" width="70" />
              <el-table-column :label="$t('workrate.authors.index')" width="150">
                <template #default="{ row }">
                  <span class="author-score">{{ row.index.toFixed(1) }}</span>
                  <el-tag :type="levelType(row.level)" size="small" class="level-tag">
                    {{ $t(`workrate.index.level.${row.level}`) }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
        <el-col :span="10" :xs="24">
          <el-card class="stack-card">
            <template #header>{{ $t('workrate.repos.title') }}</template>
            <div v-for="r in report.repos" :key="r.name" class="repo-row">
              <span class="repo-name" :title="r.name">{{ r.name }}</span>
              <span class="repo-count">{{ r.count }}</span>
            </div>
          </el-card>
          <el-card class="stack-card">
            <template #header>{{ $t('workrate.insights.title') }}</template>
            <ul class="narrative">
              <li v-for="(i, idx) in report.insights" :key="`i${idx}`">{{ insightText(i) }}</li>
            </ul>
          </el-card>
          <el-card class="stack-card">
            <template #header>{{ $t('workrate.suggestions.title') }}</template>
            <ol class="narrative">
              <li v-for="(s, idx) in report.suggestions" :key="`s${idx}`">{{ suggestionText(s) }}</li>
            </ol>
          </el-card>
        </el-col>
      </el-row>

      <div class="note">{{ $t('workrate.note') }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import * as echarts from 'echarts'
import {
  getWorkrateReport,
  getWorkrateOptions,
  type WorkrateReport,
  type WorkrateOptions,
  type WorkratePart,
} from '../api'
import { useDark } from '../composables/useDark'

const { t, locale } = useI18n()
const { isDark } = useDark()
const chartTheme = new WeakMap<HTMLDivElement, string | undefined>()

// JS getTimezoneOffset()：东八区为 -480，后端按「本地 = UTC - offset」平移分桶
const tzOffset = new Date().getTimezoneOffset()

const emptyReport = (): WorkrateReport => ({
  scope: { project_id: 0, author: '', days: 90, commits: 0, projects_count: 0, from: '', to: '' },
  kpi: {
    total: 0, active_days: 0, daily_avg: 0, longest_streak: 0,
    streak_from: '', streak_to: '', late_night: 0, late_night_pct: 0,
    night: 0, night_pct: 0, non_work: 0, non_work_pct: 0,
    weekend: 0, weekend_pct: 0, night_or_weekend: 0, night_or_weekend_pct: 0,
  },
  index: { score: 0, level: 'none', parts: [] },
  hourly: [], weekday: [], monthly: [], repos: [], authors: [], insights: [], suggestions: [],
})

const projectId = ref<number>()
const author = ref<string>()
const days = ref(90)
const loading = ref(false)
const optionData = ref<WorkrateOptions>({ authors: [], projects: [] })
const report = ref<WorkrateReport>(emptyReport())

const hourlyRef = ref<HTMLDivElement>()
const weekdayRef = ref<HTMLDivElement>()
const monthlyRef = ref<HTMLDivElement>()

// 时段带配色（与报告图例一致）：深夜红 / 清晨紫 / 工作蓝 / 晚间黄 / 23 点橙红
const BAND_COLORS: Record<string, string> = {
  deep: '#f56c6c',
  dawn: '#9254de',
  work: '#409eff',
  evening: '#f5a623',
  night23: '#fa541c',
}
const BAND_ORDER = ['deep', 'dawn', 'work', 'evening', 'night23']
const shownBands = computed(() =>
  BAND_ORDER.filter((b) => report.value.hourly.some((h) => h.band === b && h.count > 0)),
)

const projectOptions = computed(() =>
  optionData.value.projects.filter((p): p is { id: number; count: number; name: string } => p.id != null),
)

// 指数分档 → 标签类型 / 主色
function levelType(level: string): 'success' | 'primary' | 'warning' | 'danger' | 'info' {
  if (level === 'relaxed') return 'success'
  if (level === 'steady') return 'primary'
  if (level === 'intense') return 'warning'
  if (level === 'extreme') return 'danger'
  return 'info'
}
const levelTag = computed(() => levelType(report.value.index.level))
const levelText = computed(() => t(`workrate.index.level.${report.value.index.level}`))
const levelColor = computed(() => {
  const map: Record<string, string> = {
    relaxed: '#67c23a', steady: '#409eff', intense: '#e6a23c', extreme: '#f56c6c', none: '#c0c4cc',
  }
  return map[report.value.index.level] ?? '#409eff'
})

function partPct(p: WorkratePart): number {
  return p.max > 0 ? Math.round((p.score / p.max) * 100) : 0
}

// 洞察/建议：后端只给 {code, params}，星期几这类文案映射在前端补齐
function insightText(item: { code: string; params: Record<string, any> }): string {
  const params = { ...item.params }
  if (item.code === 'top_weekday') {
    params.dayName = t(`workrate.wd.${item.params.day}`)
  }
  return t(`workrate.insights.${item.code}`, params)
}
function suggestionText(item: { code: string; params: Record<string, any> }): string {
  return t(`workrate.suggestions.${item.code}`, { ...item.params })
}

function drawHourly() {
  if (!hourlyRef.value) return
  ensure(hourlyRef.value).setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, confine: true },
    grid: { containLabel: true, left: 8, right: 44, top: 8, bottom: 8 },
    xAxis: { type: 'value', minInterval: 1 },
    yAxis: {
      type: 'category',
      inverse: true,
      data: report.value.hourly.map((h) => `${String(h.hour).padStart(2, '0')}:00`),
      axisLabel: { fontSize: 11 },
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 11,
        label: { show: true, position: 'right', fontSize: 10 },
        itemStyle: { borderRadius: [0, 3, 3, 0] },
        data: report.value.hourly.map((h) => ({
          value: h.count,
          itemStyle: { color: BAND_COLORS[h.band] ?? '#409eff' },
        })),
      },
    ],
  })
}

function drawWeekday() {
  if (!weekdayRef.value) return
  ensure(weekdayRef.value).setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, confine: true },
    grid: { containLabel: true, left: 8, right: 16, top: 24, bottom: 8 },
    xAxis: {
      type: 'category',
      data: report.value.weekday.map((w) => t(`workrate.wd.${w.day}`)),
    },
    yAxis: { type: 'value', minInterval: 1, name: t('workrate.charts.axisCount'), nameGap: 10 },
    series: [
      {
        type: 'bar',
        barMaxWidth: 26,
        itemStyle: { color: '#22d3ee', borderRadius: [3, 3, 0, 0] },
        data: report.value.weekday.map((w) => w.count),
      },
    ],
  })
}

function drawMonthly() {
  if (!monthlyRef.value) return
  ensure(monthlyRef.value).setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, confine: true },
    grid: { containLabel: true, left: 8, right: 16, top: 24, bottom: 8 },
    xAxis: { type: 'category', data: report.value.monthly.map((m) => m.month) },
    yAxis: { type: 'value', minInterval: 1, name: t('workrate.charts.axisCount'), nameGap: 10 },
    series: [
      {
        type: 'bar',
        barMaxWidth: 26,
        itemStyle: { color: '#4ade80', borderRadius: [3, 3, 0, 0] },
        data: report.value.monthly.map((m) => m.count),
      },
    ],
  })
}

// 与仪表盘同款：亮/暗切换要换 echarts 主题（只能 init 时定），主题变了先 dispose
function ensure(el: HTMLDivElement) {
  const theme = isDark.value ? 'dark' : undefined
  const existing = echarts.getInstanceByDom(el)
  if (existing) {
    if (chartTheme.get(el) === theme) return existing
    existing.dispose()
  }
  chartTheme.set(el, theme)
  const inst = echarts.init(el, theme)
  inst.setOption({ backgroundColor: 'transparent' })
  return inst
}

function redrawAll() {
  drawHourly()
  drawWeekday()
  drawMonthly()
}

async function load() {
  loading.value = true
  try {
    const base = { project_id: projectId.value, days: days.value }
    const [opts, rep] = await Promise.all([
      getWorkrateOptions({ project_id: base.project_id, days: base.days }),
      getWorkrateReport({
        project_id: base.project_id,
        author: author.value || undefined,
        days: base.days,
        tz: tzOffset,
      }),
    ])
    optionData.value = opts
    report.value = rep
    await nextTick()
    redrawAll()
  } finally {
    loading.value = false
  }
}

// 窗口变化时图表 reflow；暗色/语言切换后整体重绘
function resizeCharts() {
  ;[hourlyRef, weekdayRef, monthlyRef].forEach((r) => {
    const el = r.value
    if (el) echarts.getInstanceByDom(el)?.resize()
  })
}

onMounted(() => {
  window.addEventListener('resize', resizeCharts)
  load()
})
onUnmounted(() => window.removeEventListener('resize', resizeCharts))
watch([projectId, author, days], load)
watch(isDark, () => redrawAll())
// 图表轴标签/图例是命令式 setOption，切语言后必须整体重绘
watch(locale, () => redrawAll())
</script>

<style scoped>
.filter-card {
  margin-bottom: 16px;
}
.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.filter-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.filter-item {
  width: 200px;
}
.filter-days {
  width: 130px;
}
.scope-info {
  margin-top: 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 12px;
}
@media (max-width: 1200px) {
  .kpi-grid {
    grid-template-columns: repeat(4, 1fr);
  }
}
@media (max-width: 768px) {
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
.stat-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.stat-value {
  font-size: 26px;
  font-weight: 700;
  margin-top: 6px;
}
.stat-unit {
  font-size: 13px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
  margin-left: 2px;
}
.stat-sub {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  margin-top: 2px;
}
.danger-text {
  color: var(--el-color-danger);
}
.warning-text {
  color: var(--el-color-warning);
}
.section {
  margin-top: 16px;
}
.level-tag {
  margin-left: 8px;
}
.index-row {
  display: flex;
  align-items: baseline;
  gap: 16px;
  flex-wrap: wrap;
}
.index-score {
  font-size: 44px;
  font-weight: 800;
  line-height: 1;
}
.index-score.level-relaxed {
  color: var(--el-color-success);
}
.index-score.level-steady {
  color: var(--el-color-primary);
}
.index-score.level-intense {
  color: var(--el-color-warning);
}
.index-score.level-extreme {
  color: var(--el-color-danger);
}
.index-score.level-none {
  color: var(--el-text-color-placeholder);
}
.index-desc {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.part-list {
  margin-top: 16px;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px 32px;
}
@media (max-width: 768px) {
  .part-list {
    grid-template-columns: 1fr;
  }
}
.part-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.part-label {
  width: 64px;
  flex: none;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.part-bar {
  flex: 1;
}
.part-score {
  width: 70px;
  flex: none;
  text-align: right;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.legend {
  margin-left: 12px;
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}
.legend-item {
  margin-right: 12px;
}
.legend-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 4px;
}
.chart {
  height: 300px;
  width: 100%;
  box-sizing: border-box;
}
.chart-wide {
  height: 560px;
}
.author-score {
  font-weight: 600;
  margin-right: 6px;
}
.stack-card {
  margin-bottom: 16px;
}
.repo-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
  font-size: 13px;
}
.repo-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-right: 12px;
}
.repo-count {
  flex: none;
  font-weight: 600;
}
.narrative {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.9;
  color: var(--el-text-color-regular);
}
.note {
  margin-top: 16px;
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}
</style>
