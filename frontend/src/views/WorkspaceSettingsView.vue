<template>
  <div>
    <!-- 空间解析状态：无自带 key 且未豁免 → 审查降级；配置了 → 已启用 -->
    <el-alert
      v-if="store.workspace && !hasAnyKey"
      type="warning"
      :closable="false"
      show-icon
      title="该工作区未配置自有 LLM 密钥"
      description="按「必须自带 key」策略，审查将降级/跳过，直到在下方至少配置一个模型。平台超管可在底部开启豁免以回落平台全局凭据。"
    />
    <el-alert
      v-else-if="store.workspace && hasAnyKey"
      type="success"
      :closable="false"
      show-icon
      title="该工作区已配置自有密钥"
      description="审查将使用本空间的模型与平台凭据，成本单独归属该工作区。"
    />
    <p v-if="!store.workspace" class="muted">当前账号无可属工作区（超管或未注册空间）。</p>

    <!-- 空间模型 -->
    <el-card style="margin-top: 16px">
      <template #header>工作区模型（BYOK · LLM key）</template>
      <div class="toolbar">
        <el-button type="primary" :disabled="!store.workspace" @click="openCreate">新增模型</el-button>
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
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template v-if="reviewChain.length || !store.workspace">
        <div class="chain-row" v-if="reviewChain.length">
          <span class="chain-label">当前使用</span>
          <el-tag type="primary">{{ reviewChain[0].name }}（主）</el-tag>
          <template v-for="(m, i) in reviewChain.slice(1)" :key="m.id">
            <el-tag type="info">{{ i + 1 }}. {{ m.name }}</el-tag>
          </template>
        </div>
      </template>
    </el-card>

    <!-- 模型编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑工作区模型' : '新增工作区模型'" width="640px">
      <el-form :model="form" label-width="120px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="平台" required>
          <el-select v-model="form.provider" placeholder="选择消息协议格式" style="width: 100%">
            <el-option label="OpenAI 兼容格式" value="openai" />
            <el-option label="Anthropic（Claude）" value="anthropic" />
          </el-select>
          <div class="form-tip">平台决定接口格式与密钥路由前缀（provider 前缀自动拼到模型名）。</div>
        </el-form-item>
        <el-form-item label="模型" required>
          <el-input v-model="modelBare" placeholder="如 gpt-4o / deepseek-chat / claude-sonnet-4-5" />
          <div class="form-tip">
            保存为复合名 <code>{{ resolvedComposite || '（等待输入模型名）' }}</code>
            <template v-if="form.provider">，前缀「{{ form.provider }}」自动拼接</template>
          </div>
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.base_url" placeholder="可选，如 https://api.moonshot.cn/v1" />
        </el-form-item>
        <el-form-item label="API Key" required>
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="isEdit ? '留空或填 ****** 表示不修改' : '请输入本工作区的 API Key'"
          />
          <div class="form-tip" v-if="isEdit">读回为 ****** 表示保留原值。</div>
        </el-form-item>
        <el-form-item label="温度">
          <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" />
        </el-form-item>
        <el-form-item label="最大输出 Token">
          <el-input-number v-model="form.max_tokens" :min="1" :step="1024" style="width: 200px" />
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

    <!-- 空间 forge 凭据 -->
    <el-card style="margin-top: 16px">
      <template #header>工作区平台凭据（BYOK · forge PAT）</template>
      <el-form label-width="120px" style="max-width: 640px">
        <el-form-item v-for="p in providers" :key="p.provider" :label="p.label">
          <div class="forge-block">
            <el-input v-model="p.form.url" placeholder="平台 Base URL（留空用默认）" style="margin-bottom: 8px" />
            <el-input
              v-model="p.form.token"
              type="password"
              show-password
              :placeholder="`${p.label} PAT / Token（提交 ****** 保留原值）`"
              style="margin-bottom: 8px"
            />
            <div>
              <el-button type="primary" size="small" :loading="p.saving" @click="saveForge(p)">
                {{ p.configured ? '更新' : '保存' }}
              </el-button>
              <el-button v-if="p.configured" size="small" type="danger" @click="clearForge(p)">清除</el-button>
              <span class="muted" v-if="p.configured">已配置</span>
              <span class="muted" v-else>未配置 → 该平台在该工作区不可用</span>
            </div>
          </div>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 平台豁免（超管专属；member 角色看不到） -->
    <el-card v-if="isSuper" style="margin-top: 16px">
      <template #header>平台豁免（超管）</template>
      <el-form label-width="120px" style="max-width: 640px">
        <el-form-item label="允许回落平台全局凭据">
          <el-switch v-model="fallbackEnabled" @change="onFallbackChange" />
          <div class="form-tip">开启后，该工作区缺自有 key/PAT 时可回落平台全局凭据；关闭（默认）= 必须自带 key。</div>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import {
  listWorkspaceModels,
  createWorkspaceModel,
  updateWorkspaceModel,
  deleteWorkspaceModel,
  getWorkspaceForge,
  putWorkspaceForge,
  deleteWorkspaceForge,
  getPlatformFallback,
  setPlatformFallback,
  type ModelItem,
} from '../api'

const route = useRoute()
const store = useAuthStore()

// 仅工作区 owner 才渲染（后端 require_workspace_owner 同步拦截非 owner）
const wsId = ref<number>(Number(route.params.id))
const REDACTED = '******'
const isSuper = computed(() => store.hasPerm('users:manage'))

const items = ref<ModelItem[]>([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const editingId = ref<number | null>(null)

const providers = reactive([
  { provider: 'gitlab', label: 'GitLab', form: { url: '', token: '' }, configured: false, saving: false },
  { provider: 'github', label: 'GitHub', form: { url: '', token: '' }, configured: false, saving: false },
])

const fallbackEnabled = ref(false)

const emptyForm = () => ({
  name: '',
  provider: 'openai',
  model: '',
  api_key: '',
  base_url: '',
  temperature: 0.7,
  max_tokens: 4096,
  priority: 0,
  enabled: true,
})
const form = reactive(emptyForm())

const modelBare = ref('')
function bareOf(model: string): string {
  return model.includes('/') ? model.slice(model.indexOf('/') + 1) : model
}
const resolvedComposite = computed(() => {
  const m = modelBare.value.trim()
  if (!m) return form.model
  if (m.includes('/')) return m
  return form.provider ? `${form.provider}/${m}` : m
})

const reviewChain = computed(() =>
  items.value
    .filter((m) => m.enabled)
    .slice()
    .sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || a.id - b.id),
)

const hasAnyKey = computed(() => reviewChain.value.length > 0)

function openCreate() {
  isEdit.value = false
  editingId.value = null
  modelBare.value = ''
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
    api_key: row.api_key || REDACTED,
    base_url: row.base_url || '',
    temperature: row.temperature ?? 0.7,
    max_tokens: row.max_tokens ?? 4096,
    priority: row.priority ?? 0,
    enabled: row.enabled,
  })
  modelBare.value = bareOf(row.model ?? '')
  dialogVisible.value = true
}

async function onSave() {
  if (!isEdit.value && !form.api_key) {
    ElMessage.warning('请输入 API Key')
    return
  }
  form.model = resolvedComposite.value
  saving.value = true
  try {
    if (isEdit.value && editingId.value != null) {
      await updateWorkspaceModel(wsId.value, editingId.value, { ...form })
    } else {
      await createWorkspaceModel(wsId.value, { ...form })
    }
    ElMessage.success('保存成功')
    dialogVisible.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function onDelete(row: ModelItem) {
  await ElMessageBox.confirm(`确认删除工作区模型「${row.name}」？`, '提示', { type: 'warning' })
  await deleteWorkspaceModel(wsId.value, row.id)
  ElMessage.success('已删除')
  await load()
}

async function saveForge(p: (typeof providers)[number]) {
  p.saving = true
  try {
    await putWorkspaceForge(wsId.value, p.provider, { url: p.form.url, token: p.form.token, enabled: true })
    ElMessage.success(`${p.label} 凭据已保存`)
    await loadForge(p)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    p.saving = false
  }
}

async function clearForge(p: (typeof providers)[number]) {
  await ElMessageBox.confirm(`确认清除工作区 ${p.label} 凭据？`, '提示', { type: 'warning' })
  await deleteWorkspaceForge(wsId.value, p.provider)
  p.form.url = ''
  p.form.token = ''
  p.configured = false
  ElMessage.success('已清除')
}

async function loadForge(p: (typeof providers)[number]) {
  try {
    const cfg = await getWorkspaceForge(wsId.value, p.provider)
    p.form.url = cfg.url || ''
    p.form.token = cfg.token && cfg.token !== REDACTED ? cfg.token : ''
    p.configured = !!cfg.token && cfg.token !== REDACTED
  } catch { /* 未配置 → 保持默认 */ }
}

async function load() {
  loading.value = true
  try {
    items.value = await listWorkspaceModels(wsId.value)
  } finally {
    loading.value = false
  }
}

async function onFallbackChange(val: boolean) {
  try {
    await setPlatformFallback(wsId.value, val)
    ElMessage.success(val ? '已开启平台豁免' : '已关闭平台豁免')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
    fallbackEnabled.value = !val
  }
}

onMounted(async () => {
  await load()
  for (const p of providers) await loadForge(p)
  // 读回豁免开关（owner 可读；非 owner 后端 403）
  try {
    const extra = await getPlatformFallback(wsId.value) as any
    fallbackEnabled.value = !!extra?.platform_fallback
  } catch { /* 非 owner：不读 */ }
})
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
.chain-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.chain-label {
  color: #909399;
  font-size: 13px;
  white-space: nowrap;
}
.muted {
  color: #909399;
  font-size: 12px;
}
.forge-block {
  width: 100%;
  max-width: 520px;
}
</style>