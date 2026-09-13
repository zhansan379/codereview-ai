<template>
  <div v-loading="loading">
    <!-- 筛选：日期范围（真实创建时间口径）+ 重置 -->
    <el-card class="filter-card">
      <div class="filter-row">
        <span class="filter-label">{{ $t('members.range') }}</span>
        <el-date-picker
          v-model="range"
          type="daterange"
          value-format="YYYY-MM-DD"
          :start-placeholder="$t('members.from')"
          :end-placeholder="$t('members.to')"
          :clearable="false"
          class="range-picker"
        />
        <el-button :icon="RefreshLeft" @click="resetFilters">
          {{ $t('members.reset') }}
        </el-button>
      </div>
    </el-card>

    <el-card class="table-card">
      <el-table :data="paged" stripe>
        <el-table-column :label="$t('members.member')" min-width="120">
          <template #default="{ row }">
            <span class="member-name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('members.projects')" width="100" align="center">
          <template #default="{ row }">{{ row.projects }}</template>
        </el-table-column>
        <el-table-column :label="$t('members.commits')" width="100" align="center">
          <template #default="{ row }">{{ row.commits }}</template>
        </el-table-column>
        <el-table-column :label="$t('members.dailyAvg')" width="100" align="center">
          <template #default="{ row }">{{ row.daily_avg }}</template>
        </el-table-column>
        <el-table-column :label="$t('members.trend')" min-width="220">
          <template #default="{ row }">
            <div class="trend">
              <div
                v-for="b in trendBars(row)"
                :key="b.key"
                class="trend-col"
                :title="b.tip"
              >
                <div v-if="b.count > 0" class="trend-bar" :style="{ height: b.height + '%' }" />
                <div v-else class="trend-zero" />
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="$t('members.avgScore')" width="110" align="center">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.avg_score !== null"
              :content="$t('members.scoredHint', { n: row.scored })"
              placement="top"
            >
              <span class="score-badge" :class="scoreClass(row.avg_score)">{{ row.avg_score }}</span>
            </el-tooltip>
            <el-tooltip v-else :content="$t('members.noScore')" placement="top">
              <span class="muted">—</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column :label="$t('members.codeChange')" width="150" align="center">
          <template #default="{ row }">
            <template v-if="row.additions !== null">
              <span class="add">+{{ row.additions }}</span>
              <span class="sep"> / </span>
              <span class="del">-{{ row.deletions }}</span>
            </template>
            <span v-else-if="row.changed_lines > 0" class="muted">
              {{ $t('members.changedLines', { n: row.changed_lines }) }}
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RefreshLeft } from '@element-plus/icons-vue'
import { getMembersReport, MembersReport, MemberStat } from '../api/members'

const { t } = useI18n()
const loading = ref(false)
const report = ref<MembersReport | null>(null)

// 默认最近 30 天（含今天）；重置恢复
function fmt(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}
function defaultRange(): [string, string] {
  const to = new Date()
  const from = new Date(to.getTime() - 29 * 86400000)
  return [fmt(from), fmt(to)]
}
const range = ref<[string, string]>(defaultRange())
function resetFilters() {
  range.value = defaultRange()
}

async function load() {
  loading.value = true
  try {
    report.value = await getMembersReport({
      date_from: range.value?.[0],
      date_to: range.value?.[1],
      tz: new Date().getTimezoneOffset(),
    })
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(range, load)

// 客户端分页（成员数量级小，后端一次性返回）
const page = ref(1)
const pageSize = ref(10)
const total = computed(() => report.value?.members.length ?? 0)
const paged = computed(() => {
  const list = report.value?.members ?? []
  const start = (page.value - 1) * pageSize.value
  return list.slice(start, start + pageSize.value)
})

// 趋势迷你柱：≤14 天逐日；更长按等宽分桶（最多 14 桶）
const MAX_BARS = 14
function addDays(s: string, n: number): string {
  const d = new Date(s + 'T00:00:00')
  d.setDate(d.getDate() + n)
  return fmt(d)
}
function trendBars(row: MemberStat): { key: string; count: number; height: number; tip: string }[] {
  const from = report.value?.scope.from
  const to = report.value?.scope.to
  if (!from || !to) return []
  const dayMs = 86400000
  const totalDays = Math.round((new Date(to + 'T00:00:00').getTime() - new Date(from + 'T00:00:00').getTime()) / dayMs) + 1
  const bars: { key: string; count: number; height: number; tip: string }[] = []
  if (totalDays <= MAX_BARS) {
    for (let i = 0; i < totalDays; i++) {
      const day = addDays(from, i)
      const count = row.trend[day] ?? 0
      bars.push({ key: day, count, height: 100, tip: `${day}: ${count}` })
    }
  } else {
    const size = Math.ceil(totalDays / MAX_BARS)
    for (let b = 0; b < MAX_BARS; b++) {
      const start = addDays(from, b * size)
      if (start > to) break
      let count = 0
      for (let i = 0; i < size; i++) {
        const day = addDays(start, i)
        if (day > to) break
        count += row.trend[day] ?? 0
      }
      const end = addDays(start, size - 1)
      bars.push({ key: start, count, height: 100, tip: `${start} ~ ${end < to ? end : to}: ${count}` })
    }
  }
  const max = Math.max(...bars.map((b) => b.count), 1)
  return bars.map((b) => ({ ...b, height: Math.max(Math.round((b.count / max) * 100), 12) }))
}

// 评分徽章配色：≥80 绿、≥60 黄、其余红
function scoreClass(score: number): string {
  if (score >= 80) return 'good'
  if (score >= 60) return 'mid'
  return 'bad'
}
</script>

<style scoped>
.filter-card {
  margin-bottom: 16px;
}
.filter-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.filter-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.range-picker {
  max-width: 280px;
}
.member-name {
  font-weight: 600;
}
/* 趋势迷你柱：底部对齐的小方块列，贴参考图风格 */
.trend {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 36px;
  width: 100%;
  max-width: 320px;
}
.trend-col {
  flex: 1 1 0;
  max-width: 22px;
  height: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}
.trend-bar {
  width: 70%;
  min-width: 6px;
  border-radius: 3px 3px 0 0;
  background: var(--el-color-success);
}
.trend-zero {
  width: 70%;
  min-width: 6px;
  height: 2px;
  border-radius: 1px;
  background: var(--el-border-color);
}
.score-badge {
  display: inline-block;
  min-width: 44px;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 600;
  font-size: 13px;
}
.score-badge.good {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9, rgba(103, 194, 58, 0.12));
}
.score-badge.mid {
  color: var(--el-color-warning);
  background: var(--el-color-warning-light-9, rgba(230, 162, 60, 0.12));
}
.score-badge.bad {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9, rgba(245, 108, 108, 0.12));
}
.add {
  color: var(--el-color-success);
  font-weight: 600;
}
.del {
  color: var(--el-color-danger);
  font-weight: 600;
}
.sep {
  color: var(--el-text-color-secondary);
}
.muted {
  color: var(--el-text-color-secondary);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
