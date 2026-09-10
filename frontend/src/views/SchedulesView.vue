<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">新增定时任务</el-button>
        <el-button @click="load">刷新</el-button>
        <span v-if="!workerActive" class="hint">
          运行器未启动（未配齐 LLM/平台），配置将落库，待运行器就绪后按计划生效。
        </span>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" label="任务名" min-width="140" />
        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <el-tag>{{ typeLabel(row.job_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="参数" min-width="150">
          <template #default="{ row }">
            {{ paramText(row) }}
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="210" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="onRun(row)">立即执行</el-button>
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑定时任务' : '新增定时任务'" width="560px">
      <el-form :model="form" label-width="110px">
        <el-form-item label="任务名" required>
          <el-input v-model="form.name" placeholder="如：每周一轮 GitHub 补拉" />
        </el-form-item>
        <el-form-item label="类型" required>
          <el-select v-model="form.job_type" :disabled="isEdit" style="width: 100%">
            <el-option label="主动补拉轮询 poll" value="poll" />
            <el-option label="日报 daily" value="daily" />
          </el-select>
          <div class="form-tip">任务类型创建后不可修改。</div>
        </el-form-item>
        <el-form-item v-if="form.job_type === 'poll'" label="间隔(秒)" required>
          <el-input-number v-model="form.interval_seconds" :min="1" :step="60" />
          <div class="form-tip">每隔该秒数扫一次启用项目的打开 PR/MR，仅新 head 才审。</div>
        </el-form-item>
        <el-form-item v-else label="时刻(时)" required>
          <el-input-number v-model="form.hour" :min="0" :max="23" />
          <div class="form-tip">每日此时推送代码审查日报（本地时区，0–23）。</div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listSchedules,
  createSchedule,
  updateSchedule,
  deleteSchedule,
  runSchedule,
  type ScheduleJob,
} from '../api'

const items = ref<ScheduleJob[]>([])
const workerActive = ref(true)
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const editingId = ref<number | null>(null)

const typeLabels: Record<string, string> = { poll: '主动补拉轮询', daily: '日报' }
const typeLabel = (t: string) => typeLabels[t] || t

const paramText = (row: ScheduleJob): string => {
  if (row.job_type === 'poll') return `每 ${row.params?.interval_seconds ?? '-'} 秒一轮`
  if (row.job_type === 'daily') return `每日 ${row.params?.hour ?? '-'} 点`
  return ''
}

const emptyForm = () => ({
  name: '',
  job_type: 'poll' as 'poll' | 'daily',
  enabled: true,
  interval_seconds: 3600,
  hour: 9,
})
const form = reactive(emptyForm())

async function load() {
  loading.value = true
  try {
    const data = await listSchedules()
    items.value = data.items
    workerActive.value = data.worker_active
  } finally {
    loading.value = false
  }
}

function openCreate() {
  isEdit.value = false
  editingId.value = null
  Object.assign(form, emptyForm())
  dialogVisible.value = true
}
function openEdit(row: ScheduleJob) {
  isEdit.value = true
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    job_type: row.job_type,
    enabled: row.enabled,
    interval_seconds: row.params?.interval_seconds ?? 3600,
    hour: row.params?.hour ?? 9,
  })
  dialogVisible.value = true
}

function payload() {
  return {
    name: form.name,
    job_type: form.job_type,
    enabled: form.enabled,
    params:
      form.job_type === 'poll'
        ? { interval_seconds: form.interval_seconds }
        : { hour: form.hour },
  }
}

async function onSave() {
  if (!form.name.trim()) {
    ElMessage.warning('请输入任务名')
    return
  }
  saving.value = true
  try {
    if (isEdit.value && editingId.value != null) {
      await updateSchedule(editingId.value, payload())
    } else {
      await createSchedule(payload())
    }
    ElMessage.success('已保存并热更生效')
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function onRun(row: ScheduleJob) {
  try {
    await runSchedule(row.id)
    ElMessage.success(`已在后台触发「${row.name}」一次`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '触发失败')
  }
}

async function onDelete(row: ScheduleJob) {
  await ElMessageBox.confirm(`确认删除定时任务「${row.name}」？`, '提示', { type: 'warning' })
  await deleteSchedule(row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  margin-bottom: 12px;
  align-items: center;
}
.hint {
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-color-warning);
}
.form-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
</style>