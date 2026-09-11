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
        <el-table-column :label="$t('schedules.params')" min-width="180">
          <template #default="{ row }">
            {{ paramText(row) }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.enabled')" :width="colWidth(80)">
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

    <el-dialog v-model="dialogVisible" :title="isEdit ? $t('schedules.editTitle') : $t('schedules.create')" width="600px">
      <el-form :model="form" label-width="auto">
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

        <!-- 主动补拉：间隔 + 单位 -->
        <template v-if="form.job_type === 'poll'">
          <el-form-item :label="$t('schedules.interval')" required>
            <el-input-number v-model="form.interval_value" :min="1" style="width: 140px" />
            <el-select v-model="form.interval_unit" style="width: 120px; margin-left: 8px">
              <el-option v-for="u in units" :key="u.value" :label="u.label" :value="u.value" />
            </el-select>
            <div class="form-tip">{{ $t('schedules.intervalTip') }}</div>
          </el-form-item>
        </template>

        <!-- 日报：快捷预设 / Cron 高级 -->
        <template v-else>
          <el-form-item :label="$t('schedules.schedule')" required>
            <el-radio-group v-model="form.cron_mode">
              <el-radio value="preset">{{ $t('schedules.cronPreset') }}</el-radio>
              <el-radio value="cron">{{ $t('schedules.cronAdvanced') }}</el-radio>
            </el-radio-group>
            <div v-if="form.cron_mode === 'preset'" class="preset-row">
              <el-input-number v-model="form.daily_hour" :min="0" :max="23" controls-position="right" style="width: 120px" />
              <span class="colon">:</span>
              <el-input-number v-model="form.daily_min" :min="0" :max="59" controls-position="right" style="width: 120px" />
            </div>
            <div v-else>
              <el-input v-model="form.cron" :placeholder="'0 9 * * *'" style="width: 100%" />
              <div class="form-tip">{{ $t('schedules.cronTip') }}</div>
            </div>
          </el-form-item>
        </template>

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
import { colWidth } from '../composables/useLocale'
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

// 间隔单位 → 秒
const units = [
  { value: 's', label: t('schedules.unitS'), sec: 1 },
  { value: 'm', label: t('schedules.unitM'), sec: 60 },
  { value: 'h', label: t('schedules.unitH'), sec: 3600 },
  { value: 'd', label: t('schedules.unitD'), sec: 86400 },
]

// 后端若新增 job_type，没有词条时回退显示原始值
const typeLabel = (jobType: string) =>
  te(`schedules.type.${jobType}`) ? t(`schedules.type.${jobType}`) : jobType

// 「每日 H:M」cron（分 时 * * *）→ true 则画廊展示友好文案
const DAILY_CHRON = /^(\d{1,2}) ([01]?\d|2[0-3]) \* \* \*$/

const paramText = (row: ScheduleJob): string => {
  if (row.job_type === 'poll') {
    const sec = row.params?.interval_seconds ?? 0
    const { value, unit } = splitSeconds(sec)
    return t('schedules.paramPollValue', { n: value, unit: t(`schedules.unit.${unit}`) })
  }
  if (row.job_type === 'daily') {
    const cron = row.params?.cron
    if (cron && DAILY_CHRON.test(cron)) {
      const [, min, hour] = DAILY_CHRON.exec(cron)!
      return `${t('schedules.paramDaily')} ${hour.padStart(2, '0')}:${min.padStart(2, '0')}`
    }
    return cron || t('schedules.paramNoCron')
  }
  return ''
}

// 秒数 → (值, 单位)：选能整除的最大友好单位
function splitSeconds(sec: number): { value: number; unit: string } {
  if (sec > 0 && sec % 3600 === 0) return { value: sec / 3600, unit: 'h' }
  if (sec > 0 && sec % 60 === 0) return { value: sec / 60, unit: 'm' }
  return { value: sec, unit: 's' }
}

const emptyForm = () => ({
  name: '',
  job_type: 'poll' as 'poll' | 'daily',
  enabled: true,
  interval_value: 60,
  interval_unit: 'm',
  cron_mode: 'preset' as 'preset' | 'cron',
  daily_hour: 9,
  daily_min: 0,
  cron: '0 9 * * *',
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
  if (row.job_type === 'poll') {
    const { value, unit } = splitSeconds(row.params?.interval_seconds ?? 3600)
    Object.assign(form, {
      job_type: row.job_type,
      interval_value: value,
      interval_unit: unit,
      cron_mode: 'preset',
    })
  } else {
    const cron = row.params?.cron ?? '0 9 * * *'
    const m = cron.match(DAILY_CHRON)
    Object.assign(form, {
      job_type: row.job_type,
      cron_mode: m ? 'preset' : 'cron',
      cron: cron,
      daily_hour: m ? Number(m[2]) : 9,
      daily_min: m ? Number(m[1]) : 0,
    })
  }
  Object.assign(form, { name: row.name, enabled: row.enabled })
  dialogVisible.value = true
}

function payload() {
  if (form.job_type === 'poll') {
    const unit = units.find((u) => u.value === form.interval_unit)!
    return {
      name: form.name,
      job_type: form.job_type,
      enabled: form.enabled,
      params: { interval_seconds: form.interval_value * unit.sec },
    }
  }
  const cron =
    form.cron_mode === 'cron'
      ? form.cron.trim()
      : `${String(form.daily_min).padStart(2, '0')} ${String(form.daily_hour)} * * *`
  return {
    name: form.name,
    job_type: form.job_type,
    enabled: form.enabled,
    params: { cron },
  }
}

async function onSave() {
  if (!form.name.trim()) {
    ElMessage.warning(t('schedules.nameRequired'))
    return
  }
  if (form.job_type === 'daily' && form.cron_mode === 'cron' && !form.cron.trim()) {
    ElMessage.warning(t('schedules.cronRequired'))
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
.preset-row {
  display: flex;
  align-items: center;
  margin-top: 4px;
}
.colon {
  margin: 0 8px;
  font-weight: 600;
}
</style>