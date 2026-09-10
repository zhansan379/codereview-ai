<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">{{ $t('schedules.create') }}</el-button>
        <el-button @click="load">{{ $t('common.refresh') }}</el-button>
        <span v-if="!workerActive" class="hint">
          {{ $t('schedules.workerInactive') }}
        </span>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" :label="$t('schedules.jobName')" min-width="140" />
        <el-table-column :label="$t('schedules.jobType')" width="120">
          <template #default="{ row }">
            <el-tag>{{ typeLabel(row.job_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('schedules.params')" min-width="150">
          <template #default="{ row }">
            {{ paramText(row) }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.enabled')" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? $t('common.yes') : $t('common.no') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.actions')" width="210" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="onRun(row)">{{ $t('schedules.runNow') }}</el-button>
            <el-button link type="primary" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button link type="danger" @click="onDelete(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? $t('schedules.editTitle') : $t('schedules.create')" width="560px">
      <el-form :model="form" label-width="110px">
        <el-form-item :label="$t('schedules.jobName')" required>
          <el-input v-model="form.name" :placeholder="$t('schedules.namePlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('schedules.jobType')" required>
          <el-select v-model="form.job_type" :disabled="isEdit" style="width: 100%">
            <el-option :label="$t('schedules.typePoll')" value="poll" />
            <el-option :label="$t('schedules.typeDaily')" value="daily" />
          </el-select>
          <div class="form-tip">{{ $t('schedules.typeFixed') }}</div>
        </el-form-item>
        <el-form-item v-if="form.job_type === 'poll'" :label="$t('schedules.interval')" required>
          <el-input-number v-model="form.interval_seconds" :min="1" :step="60" />
          <div class="form-tip">{{ $t('schedules.intervalTip') }}</div>
        </el-form-item>
        <el-form-item v-else :label="$t('schedules.hour')" required>
          <el-input-number v-model="form.hour" :min="0" :max="23" />
          <div class="form-tip">{{ $t('schedules.hourTip') }}</div>
        </el-form-item>
        <el-form-item :label="$t('common.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listSchedules,
  createSchedule,
  updateSchedule,
  deleteSchedule,
  runSchedule,
  type ScheduleJob,
} from '../api'

const { t, te } = useI18n()

const items = ref<ScheduleJob[]>([])
const workerActive = ref(true)
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const editingId = ref<number | null>(null)

// 后端若新增 job_type，没有词条时回退显示原始值
const typeLabel = (jobType: string) =>
  te(`schedules.type.${jobType}`) ? t(`schedules.type.${jobType}`) : jobType

const paramText = (row: ScheduleJob): string => {
  if (row.job_type === 'poll') return t('schedules.paramPoll', { n: row.params?.interval_seconds ?? '-' })
  if (row.job_type === 'daily') return t('schedules.paramDaily', { n: row.params?.hour ?? '-' })
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
    ElMessage.warning(t('schedules.nameRequired'))
    return
  }
  saving.value = true
  try {
    if (isEdit.value && editingId.value != null) {
      await updateSchedule(editingId.value, payload())
    } else {
      await createSchedule(payload())
    }
    ElMessage.success(t('schedules.savedAndApplied'))
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function onRun(row: ScheduleJob) {
  try {
    await runSchedule(row.id)
    ElMessage.success(t('schedules.triggered', { name: row.name }))
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('schedules.triggerFailed'))
  }
}

async function onDelete(row: ScheduleJob) {
  await ElMessageBox.confirm(t('schedules.deleteConfirm', { name: row.name }), t('common.tip'), {
    type: 'warning',
  })
  await deleteSchedule(row.id)
  ElMessage.success(t('common.deleted'))
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