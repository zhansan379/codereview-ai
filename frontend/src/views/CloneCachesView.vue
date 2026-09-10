<template>
  <div>
    <el-card class="policy-card">
      <template #header>
        <div class="policy-header">
          <span>{{ $t('caches.policyTitle') }}</span>
          <span v-if="prunerRunning" class="hint ok">{{ $t('caches.prunerRunning') }}</span>
          <span v-else class="hint">{{ $t('caches.prunerStopped') }}</span>
        </div>
      </template>
      <div class="policy-body">
        <el-form :inline="true" label-width="90px">
          <el-form-item :label="$t('common.enabled')">
            <el-switch v-model="settings.enabled" />
          </el-form-item>
          <el-form-item :label="$t('caches.retentionDays')">
            <el-input-number v-model="settings.days" :min="0" :max="3650" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="saving" @click="onSave">{{ $t('caches.savePolicy') }}</el-button>
            <el-button :loading="pruning" @click="onPrune">{{ $t('caches.pruneNow') }}</el-button>
          </el-form-item>
        </el-form>
        <div class="hint">
          {{ $t('caches.policyHint') }}
          {{ $t('caches.cacheRootLine', { path: cacheRoot }) }}
        </div>
      </div>
    </el-card>

    <el-card>
      <div class="toolbar">
        <span class="toolbar-title">{{ $t('caches.listTitle') }}</span>
        <el-button :loading="scanning" @click="onRebuild">{{ $t('caches.scanExisting') }}</el-button>
        <el-button @click="load">{{ $t('common.refresh') }}</el-button>
      </div>
      <div class="hint">
        <i18n-t keypath="caches.listHint" scope="global">
          <template #agentic><b>agentic</b></template>
        </i18n-t>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="repo_full_name" :label="$t('caches.repo')" min-width="220" />
        <el-table-column prop="provider" :label="$t('caches.provider')" width="90" />
        <el-table-column :label="$t('caches.lastFetchedHead')" width="220">
          <template #default="{ row }">
            <code class="sha">{{ shortSha(row.head_sha) }}</code>
          </template>
        </el-table-column>
        <el-table-column :label="$t('caches.lastFetched')" width="170">
          <template #default="{ row }">
            {{ fmt(row.last_fetched_at) }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('caches.firstCached')" width="170">
          <template #default="{ row }">
            {{ fmt(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.actions')" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" @click="onDelete(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="$t('caches.emptyDesc')" />
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import {
  listCloneCaches,
  deleteCloneCache,
  updateCloneCacheSettings,
  pruneCloneCaches,
  rebuildCloneCaches,
  type CloneCacheItem,
  type CloneCacheSettings,
} from '../api'

const { t } = useI18n()

const items = ref<CloneCacheItem[]>([])
const cacheRoot = ref('')
const prunerRunning = ref(false)
const loading = ref(false)
const saving = ref(false)
const pruning = ref(false)
const scanning = ref(false)
const settings = reactive<CloneCacheSettings>({ enabled: false, days: 30 })

const fmt = (s: string): string => (s ? new Date(s).toLocaleString() : '-')
const shortSha = (s: string): string => (s ? s.slice(0, 8) : '-')

async function load() {
  loading.value = true
  try {
    const data = await listCloneCaches()
    items.value = data.items
    cacheRoot.value = data.cache_root
    prunerRunning.value = data.pruner_running
    settings.enabled = data.enabled
    settings.days = data.days
  } finally {
    loading.value = false
  }
}

async function onSave() {
  saving.value = true
  try {
    await updateCloneCacheSettings({ enabled: settings.enabled, days: settings.days })
    ElMessage.success(t('caches.policySaved'))
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function onPrune() {
  pruning.value = true
  try {
    const res = await pruneCloneCaches()
    ElMessage.success(res.count > 0 ? t('caches.pruned', { n: res.count }) : t('caches.nothingToPrune'))
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('caches.pruneFailed'))
  } finally {
    pruning.value = false
  }
}

async function onRebuild() {
  scanning.value = true
  try {
    const res = await rebuildCloneCaches()
    ElMessage.success(
      res.added > 0 ? t('caches.scanned', { n: res.added }) : t('caches.scanNoNew'),
    )
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('caches.scanFailed'))
  } finally {
    scanning.value = false
  }
}

async function onDelete(row: CloneCacheItem) {
  await ElMessageBox.confirm(t('caches.deleteConfirm', { name: row.repo_full_name }), t('common.tip'), {
    type: 'warning',
  })
  try {
    await deleteCloneCache(row.id)
    ElMessage.success(t('common.deleted'))
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.deleteFailed'))
  }
}

onMounted(load)
</script>

<style scoped>
.policy-card {
  margin-bottom: 16px;
}
.policy-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.policy-body .hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.hint.ok {
  font-size: 12px;
  color: var(--el-color-success);
}
.hint {
  font-size: 12px;
  color: var(--el-color-warning);
}
.toolbar {
  margin-bottom: 12px;
}
.toolbar-title {
  font-weight: 600;
  margin-right: 12px;
}
.sha {
  font-family: monospace;
  font-size: 12px;
  background: var(--el-fill-color);
  padding: 0 4px;
  border-radius: 3px;
}
</style>