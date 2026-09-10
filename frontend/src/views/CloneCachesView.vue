<template>
  <div>
    <el-card class="policy-card">
      <template #header>
        <div class="policy-header">
          <span>清除策略</span>
          <span v-if="prunerRunning" class="hint ok">清除策略循环运行中</span>
          <span v-else class="hint">清除策略循环未启动</span>
        </div>
      </template>
      <div class="policy-body">
        <el-form :inline="true" label-width="90px">
          <el-form-item label="启用">
            <el-switch v-model="settings.enabled" />
          </el-form-item>
          <el-form-item label="保留天数">
            <el-input-number v-model="settings.days" :min="0" :max="3650" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="saving" @click="onSave">保存策略</el-button>
            <el-button :loading="pruning" @click="onPrune">立即清理</el-button>
          </el-form-item>
        </el-form>
        <div class="hint">
          启用后，清除策略循环每 6 小时自查一次，删除「最近拉取」早于保留天数的缓存仓库及其 .git 目录；
          保留天数设为 0 表示清理全部。“立即清理”无视开关，按当前保留天数立即清一次。
          缓存根目录：{{ cacheRoot }}
        </div>
      </div>
    </el-card>

    <el-card>
      <div class="toolbar">
        <span class="toolbar-title">最近拉取信息</span>
        <el-button :loading="scanning" @click="onRebuild">扫描存量目录</el-button>
        <el-button @click="load">刷新</el-button>
      </div>
      <div class="hint">
        说明：仅当项目采用 <b>agentic</b> 审查策略时才会经 agent 拉取仓库并在本页登记；采用 diff 策略的项目不拉取、不出现在这里。
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="repo_full_name" label="仓库" min-width="220" />
        <el-table-column prop="provider" label="平台" width="90" />
        <el-table-column label="最近拉取 head" width="220">
          <template #default="{ row }">
            <code class="sha">{{ shortSha(row.head_sha) }}</code>
          </template>
        </el-table-column>
        <el-table-column label="最近拉取" width="170">
          <template #default="{ row }">
            {{ fmt(row.last_fetched_at) }}
          </template>
        </el-table-column>
        <el-table-column label="首次缓存" width="170">
          <template #default="{ row }">
            {{ fmt(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无缓存记录。agent 对某仓库做一次克隆/拉取后这里会出现一行。" />
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listCloneCaches,
  deleteCloneCache,
  updateCloneCacheSettings,
  pruneCloneCaches,
  rebuildCloneCaches,
  type CloneCacheItem,
  type CloneCacheSettings,
} from '../api'

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
    ElMessage.success('已保存清除策略')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function onPrune() {
  pruning.value = true
  try {
    const res = await pruneCloneCaches()
    ElMessage.success(res.count > 0 ? `已清理 ${res.count} 个缓存仓库` : '当前没有超期缓存')
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '清理失败')
  } finally {
    pruning.value = false
  }
}

async function onRebuild() {
  scanning.value = true
  try {
    const res = await rebuildCloneCaches()
    ElMessage.success(
      res.added > 0 ? `已把 ${res.added} 个存量目录纳入列表` : '无新增（均已登记）',
    )
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '扫描失败')
  } finally {
    scanning.value = false
  }
}

async function onDelete(row: CloneCacheItem) {
  await ElMessageBox.confirm(`确认删除缓存仓库「${row.repo_full_name}」？`, '提示', { type: 'warning' })
  try {
    await deleteCloneCache(row.id)
    ElMessage.success('已删除')
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
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