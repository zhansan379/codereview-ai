<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">新增模型</el-button>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column prop="provider" label="平台" width="110" />
        <el-table-column prop="model" label="模型" min-width="140" />
        <el-table-column prop="base_url" label="Base URL" min-width="160" show-overflow-tooltip />
        <el-table-column prop="priority" label="优先级" width="90" />
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="onTest(row)">连通测试</el-button>
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑模型' : '新增模型'" width="640px">
      <el-form :model="form" label-width="120px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="平台" required>
          <el-input v-model="form.provider" placeholder="如 openai / deepseek" />
        </el-form-item>
        <el-form-item label="模型" required>
          <el-input v-model="form.model" placeholder="如 gpt-4o" />
        </el-form-item>
        <el-form-item label="API Key" required>
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="isEdit ? '留空或填 ****** 表示不修改' : '请输入 API Key'"
          />
          <div class="form-tip" v-if="isEdit">读回为 ****** 表示保留原值。</div>
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.base_url" placeholder="可选，默认后端配置" />
        </el-form-item>
        <el-form-item label="温度">
          <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" />
        </el-form-item>
        <el-form-item label="最大 Token">
          <el-input-number v-model="form.max_tokens" :min="1" />
        </el-form-item>
        <el-form-item label="能力">
          <el-select v-model="form.capabilities" multiple placeholder="选择能力" style="width: 100%">
            <el-option label="审查" value="review" />
            <el-option label="总结" value="summary" />
            <el-option label="测试" value="test" />
          </el-select>
        </el-form-item>
        <el-form-item label="优先级">
          <el-input-number v-model="form.priority" :min="0" />
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
import { listModels, createModel, updateModel, deleteModel, testModel, type ModelItem } from '../api'

const items = ref<ModelItem[]>([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const editingId = ref<number | null>(null)

const REDACTED = '******'

const emptyForm = () => ({
  name: '',
  provider: '',
  model: '',
  api_key: '',
  base_url: '',
  temperature: 0.7,
  max_tokens: 2000,
  capabilities: [] as string[],
  priority: 0,
  enabled: true,
})
const form = reactive(emptyForm())

async function load() {
  loading.value = true
  try {
    items.value = await listModels()
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
function openEdit(row: ModelItem) {
  isEdit.value = true
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    provider: row.provider,
    model: row.model,
    // 密钥读回为 ****** 或 ''，作为占位值
    api_key: row.api_key || REDACTED,
    base_url: row.base_url || '',
    temperature: row.temperature ?? 0.7,
    max_tokens: row.max_tokens ?? 2000,
    capabilities: row.capabilities || [],
    priority: row.priority ?? 0,
    enabled: row.enabled,
  })
  dialogVisible.value = true
}

async function onSave() {
  // 新建时 api_key 必填
  if (!isEdit.value && !form.api_key) {
    ElMessage.warning('请输入 API Key')
    return
  }
  saving.value = true
  try {
    if (isEdit.value && editingId.value != null) {
      // 若仍是占位 ****** 则原样提交，后端按「不修改」处理
      await updateModel(editingId.value, { ...form })
    } else {
      await createModel({ ...form })
    }
    ElMessage.success('保存成功')
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

// 连通测试：失败时用 axios 的 error.response.data.detail 取后端消息
async function onTest(row: ModelItem) {
  if (!row.api_key) {
    // 后端可能不返回 key，仍需尝试；若 401 由拦截器统一处理
  }
  try {
    await testModel(row.id)
    ElMessage.success(`模型「${row.name}」连通正常`)
  } catch (e: any) {
    const msg = e?.response?.data?.detail || '连通测试失败'
    ElMessage.error(msg)
  }
}

async function onDelete(row: ModelItem) {
  await ElMessageBox.confirm(`确认删除模型「${row.name}」？`, '提示', { type: 'warning' })
  await deleteModel(row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  margin-bottom: 12px;
}
.form-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}
</style>