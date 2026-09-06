<template>
  <div>
    <el-card class="filter-card">
      <el-form inline>
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
        <el-button type="primary" @click="onFilterChange">刷新</el-button>
      </el-form>
    </el-card>

    <el-card>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="provider" label="平台" width="90" />
        <el-table-column prop="repo_id" label="仓库 ID" min-width="120" />
        <el-table-column prop="pr_number" label="PR" width="80" />
        <el-table-column prop="event_type" label="事件类型" width="110" />
        <el-table-column prop="branch" label="分支" width="100" />
        <el-table-column prop="attempt" label="尝试次数" width="90" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="stateTagType(row.state)">{{ stateLabel(row.state) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="排队时间" width="150">
          <template #default="{ row }">{{ formatTime(row.queued_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.state === 'failed'"
              link
              type="danger"
              :loading="retryingId === row.id"
              @click="onRetry(row)"
            >
              重试
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { listTasks, retryTask, type TaskItem } from '../api'
import { formatTime, stateTagType, stateLabel } from '../utils/format'

const items = ref<TaskItem[]>([])
const loading = ref(false)
const retryingId = ref<number | null>(null)
const query = reactive({ state: '' })

// 服务端按 state 过滤
async function load() {
  loading.value = true
  try {
    items.value = await listTasks({ state: query.state || undefined })
  } finally {
    loading.value = false
  }
}

function onFilterChange() {
  load()
}

// 重试失败任务，成功后刷新列表
async function onRetry(row: TaskItem) {
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
</style>