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
        <el-form-item label="预设提供商">
          <el-select
            v-model="presetKey"
            placeholder="选择常见提供商自动填充，或用「自定义」手动填写"
            style="width: 100%"
            clearable
            @change="applyPreset"
          >
            <el-option-group label="主流云厂商">
              <el-option v-for="p in cloudPresets" :key="p.key" :label="p.label" :value="p.key" />
            </el-option-group>
            <el-option-group label="自托管 / 本地">
              <el-option v-for="p in localPresets" :key="p.key" :label="p.label" :value="p.key" />
            </el-option-group>
            <el-option label="自定义（手动填写）" value="custom" />
          </el-select>
          <div class="form-tip">选中预设会填入平台 / 模型 / Base URL，可再手动微调；API Key 仍需自行填写。</div>
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

// 常见模型提供商预设：选中后自动填 provider / model / base_url。
// model 用 LiteLLM 的「provider/model」复合名（DESIGN §2）；本地/自托管带默认 Base URL。
interface ProviderPreset {
  key: string
  label: string
  provider: string
  model: string
  baseUrl: string
}

const cloudPresets: ProviderPreset[] = [
  { key: 'openai', label: 'OpenAI', provider: 'openai', model: 'openai/gpt-4o-mini', baseUrl: '' },
  { key: 'anthropic', label: 'Anthropic Claude', provider: 'anthropic', model: 'anthropic/claude-sonnet-4-5', baseUrl: '' },  // prettier-ignore
  { key: 'deepseek', label: 'DeepSeek', provider: 'deepseek', model: 'deepseek/deepseek-chat', baseUrl: '' },
  { key: 'qwen', label: '阿里云百炼 Qwen', provider: 'openai', model: 'openai/qwen-plus', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { key: 'zhipu', label: '智谱 GLM', provider: 'openai', model: 'openai/glm-4-plus', baseUrl: 'https://open.bigmodel.cn/api/paas/v4' },
  { key: 'moonshot', label: 'Moonshot（Kimi）', provider: 'openai', model: 'openai/moonshot-v1-8k', baseUrl: 'https://api.moonshot.cn/v1' },
]

const localPresets: ProviderPreset[] = [
  { key: 'ollama', label: 'Ollama', provider: 'ollama', model: 'ollama/llama3', baseUrl: 'http://localhost:11434' },
  { key: 'vllm', label: 'vLLM', provider: 'openai', model: 'openai/<模型ID>', baseUrl: 'http://localhost:8000/v1' },
  { key: 'lmstudio', label: 'LM Studio', provider: 'openai', model: 'openai/<模型名>', baseUrl: 'http://localhost:1234/v1' },
]

const allPresets = [...cloudPresets, ...localPresets]

// 表单里选中的预设 key；自定义则置空，不改动表单
const presetKey = ref<string>('')

function applyPreset() {
  const key = presetKey.value
  if (!key || key === 'custom') {
    if (key === 'custom') {
      presetKey.value = ''
      ElMessage.info('请手动填写平台 / 模型 / Base URL')
    }
    return
  }
  const p = allPresets.find((x) => x.key === key)
  if (!p) return
  form.provider = p.provider
  form.model = p.model
  form.base_url = p.baseUrl
}

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
  presetKey.value = ''
  Object.assign(form, emptyForm())
  dialogVisible.value = true
}
function openEdit(row: ModelItem) {
  isEdit.value = true
  editingId.value = row.id
  presetKey.value = ''
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