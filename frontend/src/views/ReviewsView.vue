<template>
  <div>
    <!-- 服务端过滤：按 state 筛选 -->
    <el-card class="filter-card">
      <el-form inline class="filter-form" @submit.prevent>
        <el-form-item label="状态">
          <el-select
            v-model="query.state"
            placeholder="全部状态"
            clearable
            style="width: 160px"
            @change="onFilterChange"
          >
            <el-option label="排队中" value="queued" />
            <el-option label="审查成功" value="completed" />
            <el-option label="已跳过" value="skipped" />
            <el-option label="失败" value="failed" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="onFilterChange">刷新</el-button>
        </el-form-item>
        <el-form-item class="spacer" />
        <el-form-item>
          <el-button type="success" :icon="Download" @click="openExport">导出 Excel</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- Excel 导出：范围(当前筛选) + 问题过滤(严重度/状态) -->
    <el-dialog
      v-model="exportVisible"
      title="导出问题明细"
      width="480px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-form label-width="90px">
        <el-form-item label="导出范围">
          <span class="export-scope">
            当前筛选命中的全部评审的问题明细
            <el-tag v-if="query.state" size="small" type="info">{{ stateText }}</el-tag>
          </span>
        </el-form-item>
        <el-form-item label="严重度">
          <el-select
            v-model="exportForm.severities"
            multiple
            collapse-tags
            collapse-tags-tooltip
            clearable
            placeholder="全部严重度"
            style="width: 100%"
          >
            <el-option v-for="o in severityOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-select
            v-model="exportForm.statuses"
            multiple
            collapse-tags
            collapse-tags-tooltip
            clearable
            placeholder="全部状态"
            style="width: 100%"
          >
            <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="导出内容">
          <span class="export-scope">每条问题一行，含所属评审信息（PR号/仓库/分支）及</span>
          <span class="export-scope">问题标题、详细分析、原代码、建议修复。</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="exportVisible = false">取消</el-button>
        <el-button type="primary" :loading="exporting" @click="doExport">导出</el-button>
      </template>
    </el-dialog>

    <el-card>
      <ReviewsTable
        :items="items"
        :loading="loading"
        show-process
        show-retry
        :retrying-id="retryingId"
        @detail="goDetail"
        @retry="onRetry"
      />

      <!-- 服务端分页 -->
      <el-pagination
        class="pager"
        layout="total, prev, pager, next, sizes"
        :total="total"
        :page-size="query.limit"
        :current-page="query.offset / query.limit + 1"
        :page-sizes="[10, 20, 50]"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { Download } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { listReviews, retryTask, exportReviews, type ReviewItem } from '../api'
import ReviewsTable from '../components/ReviewsTable.vue'

const router = useRouter()

const items = ref<ReviewItem[]>([])
const total = ref(0)
const loading = ref(false)
const retryingId = ref<number | null>(null)
const query = reactive({ state: '', limit: 10, offset: 0 })

// Excel 导出对话框状态
const exportVisible = ref(false)
const exporting = ref(false)
const exportForm = reactive<{ severities: string[]; statuses: string[] }>({
  severities: [],
  statuses: [],
})
const severityOptions = [
  { value: 'critical', label: '严重' },
  { value: 'high', label: '高' },
  { value: 'error', label: '错误' },
  { value: 'medium', label: '中' },
  { value: 'warning', label: '警告' },
  { value: 'low', label: '低' },
  { value: 'info', label: '提示' },
]
const statusOptions = [
  { value: 'active', label: '待处理' },
  { value: 'resolved', label: '已解决' },
  { value: 'waived', label: '已搁置' },
]
const stateOptions: Record<string, string> = {
  queued: '排队中',
  completed: '审查成功',
  skipped: '已跳过',
  failed: '失败',
}
const stateText = computed(() =>
  query.state ? stateOptions[query.state] || query.state : '',
)

function openExport() {
  exportForm.severities = []
  exportForm.statuses = []
  exportVisible.value = true
}

function pad(n: number) {
  return String(n).padStart(2, '0')
}
function ts() {
  const d = new Date()
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
}

async function doExport() {
  exporting.value = true
  try {
    const blob = await exportReviews({
      state: query.state || undefined,
      severities: exportForm.severities.length ? exportForm.severities : undefined,
      statuses: exportForm.statuses.length ? exportForm.statuses : undefined,
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `评审问题导出_${ts()}.xlsx`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    exportVisible.value = false
    ElMessage.success('导出成功')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '导出失败')
  } finally {
    exporting.value = false
  }
}

// 翻页/过滤均重新请求后端（服务端分页）
async function load() {
  loading.value = true
  try {
    const res = await listReviews({
      state: query.state || undefined,
      limit: query.limit,
      offset: query.offset,
    })
    items.value = res.items || []
    total.value = res.total || 0
  } finally {
    loading.value = false
  }
}

function onFilterChange() {
  query.offset = 0
  load()
}
function onPageChange(p: number) {
  query.offset = (p - 1) * query.limit
  load()
}
function onSizeChange(s: number) {
  query.limit = s
  query.offset = 0
  load()
}
function goDetail(id: number) {
  router.push(`/reviews/${id}`)
}

// 重试失败任务（合并自原任务页）：成功后刷新当前页
async function onRetry(row: ReviewItem) {
  retryingId.value = row.id
  try {
    await retryTask(row.id)
    ElMessage.success('已提交重试')
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重试失败')
  } finally {
    retryingId.value = null
  }
}

onMounted(load)
</script>

<style scoped>
.filter-card {
  margin-bottom: 16px;
}
.filter-form {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  row-gap: 4px;
  column-gap: 12px;
}
.filter-form .el-form-item {
  margin: 0;
}
.filter-form .spacer {
  flex: 1;
}
.export-scope {
  font-size: 13px;
  color: #606266;
  line-height: 1.6;
}
.export-scope + .export-scope {
  display: block;
}
.pager {
  margin-top: 16px;
  justify-content: flex-end;
}
</style>