<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">{{ $t('models.create') }}</el-button>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" :label="$t('models.colName')" min-width="120" />
        <el-table-column prop="provider" :label="$t('models.colProvider')" width="110" />
        <el-table-column prop="model" :label="$t('models.colModel')" min-width="140" />
        <el-table-column prop="base_url" label="Base URL" min-width="160" show-overflow-tooltip />
        <el-table-column prop="priority" :label="$t('models.colPriority')" width="90" />
        <el-table-column :label="$t('models.colEnabled')" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? $t('common.yes') : $t('common.no') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.actions')" width="220" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="onTest(row)">{{ $t('models.test') }}</el-button>
            <el-button link type="primary" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button link type="danger" @click="onDelete(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? $t('models.editTitle') : $t('models.create')"
      width="640px"
    >
      <el-form :model="form" label-width="120px">
        <el-form-item :label="$t('models.colName')" required>
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item :label="$t('models.colProvider')" required>
          <el-select
            v-model="platformKey"
            :placeholder="$t('models.platformPlaceholder')"
            style="width: 100%"
            @change="applyPlatform"
          >
            <el-option-group :label="$t('models.groupCloud')">
              <el-option v-for="p in cloudPresets" :key="p.key" :label="p.label" :value="p.key" />
            </el-option-group>
            <el-option-group :label="$t('models.groupLocal')">
              <el-option v-for="p in localPresets" :key="p.key" :label="p.label" :value="p.key" />
            </el-option-group>
            <el-option :label="$t('models.custom')" value="__custom__" />
          </el-select>
          <template v-if="platformKey === '__custom__'">
            <el-select
              v-model="form.provider"
              :placeholder="$t('models.protocolPlaceholder')"
              style="width: 100%; margin-top: 8px"
            >
              <el-option v-for="o in customProviderOptions" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
          </template>
          <div class="form-tip">
            {{ $t('models.platformTip') }}
            <template v-if="presetFormat">
              <el-tag size="small" :type="presetFormat === 'anthropic' ? 'warning' : 'success'" style="margin-left: 8px">
                {{ formatLabel[presetFormat] }}
              </el-tag>
            </template>
          </div>
        </el-form-item>
        <el-form-item :label="$t('models.colModel')" required>
          <el-input v-model="modelBare" :placeholder="$t('models.modelPlaceholder')" />
          <div class="form-tip">
            {{ $t('models.compositeTip') }}
            <code>{{ resolvedComposite || $t('models.compositeWaiting') }}</code>
            <template v-if="form.provider">{{
              $t('models.prefixTip', { provider: form.provider })
            }}</template>
          </div>
        </el-form-item>
        <el-form-item label="API Key" required>
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="isEdit ? $t('models.apiKeyKeep') : $t('models.apiKeyRequired')"
          />
          <div class="form-tip" v-if="isEdit">{{ $t('models.apiKeyRedactedTip') }}</div>
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.base_url" :placeholder="$t('models.baseUrlPlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('models.temperature')">
          <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" />
        </el-form-item>
        <el-form-item>
          <template #label>
            <el-tooltip effect="dark" placement="top">
              <template #content>
                <!-- eslint-disable-next-line vue/no-v-html —— 词条由本仓维护，非用户输入 -->
                <div style="line-height: 1.6">
                  <span v-html="$t('models.maxTokensTip1')" /><br />
                  {{ $t('models.maxTokensTip2') }}
                </div>
              </template>
              <span>
                {{ $t('models.maxTokens') }}
                <el-icon style="vertical-align: -2px"><QuestionFilled /></el-icon>
              </span>
            </el-tooltip>
          </template>
          <div class="token-editor">
            <el-input-number v-model="tokenValue" :min="1" :controls="false" style="width: 140px" />
            <el-select v-model="tokenUnit" style="width: 150px">
              <el-option label="Tokens" value="" />
              <el-option label="K (1024)" value="K" />
              <el-option label="M (1024²)" value="M" />
            </el-select>
            <span class="form-tip token-eq">≈ {{ tokenTotalText }} tokens</span>
          </div>
          <div class="token-presets">
            <el-tag
              v-for="c in COMMON_TOKENS"
              :key="c.label"
              class="token-chip"
              :effect="tokenUnit === c.unit && tokenValue === c.value ? 'dark' : 'plain'"
              clickable
              @click="pickToken(c)"
            >
              {{ c.label }}
            </el-tag>
          </div>
        </el-form-item>
        <el-form-item :label="$t('models.colPriority')">
          <el-input-number v-model="form.priority" :min="0" />
        </el-form-item>
        <el-form-item :label="$t('models.colEnabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <el-card class="cap-card">
      <template #header>{{ $t('models.chainTitle') }}</template>
      <div class="chain-row">
        <span class="chain-label">{{ $t('models.chainCurrent') }}</span>
        <el-tag v-if="reviewChain.length" type="primary">
          {{ reviewChain[0].name }}{{ $t('models.chainPrimary') }}
        </el-tag>
        <span v-else class="muted">{{ $t('models.chainNone') }}</span>
      </div>
      <div v-if="reviewChain.length > 1" class="chain-row">
        <span class="chain-label">{{ $t('models.chainFallback') }}</span>
        <el-tag
          v-for="(m, i) in reviewChain.slice(1)"
          :key="m.id"
          type="info"
          class="fallback-tag"
        >
          {{ i + 1 }}. {{ m.name }}
        </el-tag>
      </div>
      <p class="muted rule-note">{{ $t('models.chainNote') }}</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { QuestionFilled } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { listModels, createModel, updateModel, deleteModel, testModel, type ModelItem } from '../api'

const { t } = useI18n()

const items = ref<ModelItem[]>([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const editingId = ref<number | null>(null)

const REDACTED = '******'

// 常见模型提供商预设：选中后自动填 provider / model / base_url。
// model 用 LiteLLM 的「provider/model」复合名（DESIGN §2）；本地/自托管带默认 Base URL。
// format：预设走的线格式——openai=OpenAI 兼容、anthropic=Anthropic 格式、other=本地/自托管协议。
// 它是前端写死的预设元数据，映射到 model 的前缀（provider 驱动 litellm 路由与接口鉴权）。
interface ProviderPreset {
  key: string
  label: string
  provider: string
  model: string
  baseUrl: string
  format: 'openai' | 'anthropic' | 'other'
}

const cloudPresets: ProviderPreset[] = [
  { key: 'openai', label: 'OpenAI', provider: 'openai', model: 'openai/gpt-4o-mini', baseUrl: '', format: 'openai' },
  { key: 'anthropic', label: 'Anthropic Claude', provider: 'anthropic', model: 'anthropic/claude-sonnet-4-5', baseUrl: '', format: 'anthropic' },  // prettier-ignore
  // DeepSeek：走 OpenAI 兼容格式 + 官方稳定端点（/chat/completions），不落到 deepseek provider 的 /beta。
  { key: 'deepseek', label: 'DeepSeek', provider: 'openai', model: 'openai/deepseek-chat', baseUrl: 'https://api.deepseek.com', format: 'openai' },
  { key: 'qwen', label: '阿里云百炼 Qwen', provider: 'openai', model: 'openai/qwen-plus', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', format: 'openai' },
  { key: 'zhipu', label: '智谱 GLM', provider: 'openai', model: 'openai/glm-4-plus', baseUrl: 'https://open.bigmodel.cn/api/paas/v4', format: 'openai' },
  { key: 'moonshot', label: 'Moonshot（Kimi）', provider: 'openai', model: 'openai/moonshot-v1-8k', baseUrl: 'https://api.moonshot.cn/v1', format: 'openai' },
]

// 自托管预设的 model 里带占位符（<模型ID> 等），故随语言变化 → computed。
const localPresets = computed<ProviderPreset[]>(() => [
  { key: 'ollama', label: 'Ollama', provider: 'ollama', model: 'ollama/llama3', baseUrl: 'http://localhost:11434', format: 'other' },
  { key: 'vllm', label: 'vLLM', provider: 'openai', model: `openai/${t('models.modelIdPlaceholder')}`, baseUrl: 'http://localhost:8000/v1', format: 'openai' },
  { key: 'lmstudio', label: 'LM Studio', provider: 'openai', model: `openai/${t('models.modelNamePlaceholder')}`, baseUrl: 'http://localhost:1234/v1', format: 'openai' },
])

const allPresets = computed(() => [...cloudPresets, ...localPresets.value])

// 自定义平台时可选的路由前缀——只有两种消息协议格式：OpenAI 兼容 / Anthropic。
// 其余厂商一律用「openai + 各自的 Base URL」或「anthropic」表达。
const customProviderOptions = computed(() => [
  { value: 'openai', label: t('models.protocol.openai') },
  { value: 'anthropic', label: t('models.protocol.anthropic') },
])

// 「平台」下拉：值=厂商 key（或 __custom__），选中自动填路由前缀 / Base URL / 格式。
const platformKey = ref<string>('')

// 当前选中平台的线格式（openai / anthropic / other），用于表单内显式标注
const presetFormat = ref<ProviderPreset['format'] | ''>('')

// 格式标签
const formatLabel = computed<Record<Exclude<ProviderPreset['format'], ''>, string>>(() => ({
  openai: t('models.format.openai'),
  anthropic: t('models.format.anthropic'),
  other: t('models.format.other'),
}))

// 模型输入框展示「裸模型名」（不带协议前缀）；form.model 保存为带前缀的复合名。
const modelBare = ref<string>('')

// 去掉复合名里的前缀段：openai/gpt-4o-mini → gpt-4o-mini
function bareOf(model: string): string {
  return model.includes('/') ? model.slice(model.indexOf('/') + 1) : model
}

// 保存时要写入 form.model 的复合名（前缀自动拼接）
const resolvedComposite = computed(() => {
  const m = modelBare.value.trim()
  if (!m) return form.model
  if (m.includes('/')) return m // 用户已显式留了斜杠，不重复加前缀
  return form.provider ? `${form.provider}/${m}` : m
})

function applyPlatform() {
  const key = platformKey.value
  if (!key || key === '__custom__') {
    if (key === '__custom__') {
      presetFormat.value = '' // 手动填路由前缀
      // 不覆盖 form.provider / base_url，交用户自定义；已有 model 时仅去掉旧前缀便于重填
      modelBare.value = modelBare.value || bareOf(form.model)
    }
    return
  }
  const p = allPresets.value.find((x) => x.key === key)
  if (!p) return
  form.provider = p.provider
  form.base_url = p.baseUrl
  presetFormat.value = p.format
  modelBare.value = bareOf(p.model)
}

// 编辑回显：按 provider+baseUrl 尽量反向匹配到某个厂商预设，否则退回自定义
function matchPlatformKey(): string {
  const cand = allPresets.value.find(
    (p) => p.provider === form.provider && (p.baseUrl || '') === (form.base_url || ''),
  )
  return cand ? cand.key : '__custom__'
}

const emptyForm = () => ({
  name: '',
  provider: '',
  model: '',
  api_key: '',
  base_url: '',
  temperature: 0.7,
  max_tokens: 4096,
  priority: 0,
  enabled: true,
})
const form = reactive(emptyForm())

// ── 最大 Token 单位输入（原始 tokens / K / M）──────────────────────────
// 换算用 1024 基数：128K=131072，与常见模型上下文（DeepSeek/Ollama/Qwen 等）口径一致。
type TokenUnit = '' | 'K' | 'M'
const TOKEN_MULT: Record<TokenUnit, number> = { '': 1, K: 1024, M: 1024 * 1024 }
const tokenValue = ref(4)
const tokenUnit = ref<TokenUnit>('K')

const COMMON_TOKENS: { label: string; unit: TokenUnit; value: number }[] = [
  { label: '4K', unit: 'K', value: 4 },
  { label: '8K', unit: 'K', value: 8 },
  { label: '16K', unit: 'K', value: 16 },
  { label: '32K', unit: 'K', value: 32 },
  { label: '64K', unit: 'K', value: 64 },
  { label: '128K', unit: 'K', value: 128 },
  { label: '256K', unit: 'K', value: 256 },
  { label: '1M', unit: 'M', value: 1 },
]

const tokenTotal = computed(() => Math.round((tokenValue.value || 0) * TOKEN_MULT[tokenUnit.value]))
const tokenTotalText = computed(() => tokenTotal.value.toLocaleString('en-US'))

function pickToken(c: (typeof COMMON_TOKENS)[number]) {
  tokenValue.value = c.value
  tokenUnit.value = c.unit
}

// 把后端已存的原生 token 数反推成「数值 + 单位」的显示形式
function fitTokenUnit(raw: number) {
  if (raw >= TOKEN_MULT.M && raw % TOKEN_MULT.M === 0) {
    tokenUnit.value = 'M'
    tokenValue.value = raw / TOKEN_MULT.M
  } else if (raw >= TOKEN_MULT.K && raw % TOKEN_MULT.K === 0) {
    tokenUnit.value = 'K'
    tokenValue.value = raw / TOKEN_MULT.K
  } else {
    tokenUnit.value = ''
    tokenValue.value = raw
  }
}

// ── 审查回退链（按 priority 降序，与后端 resolve_llm_chain 一致）────
// 审查取所有启用的模型，排序后第 1 个为主、其余为回退。保存后 load() 重拉自动刷新。
const reviewChain = computed(() =>
  items.value
    .filter((m) => m.enabled)
    .slice()
    .sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || a.id - b.id),
)

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
  platformKey.value = ''
  presetFormat.value = ''
  modelBare.value = ''
  Object.assign(form, emptyForm())
  tokenValue.value = 4
  tokenUnit.value = emptyForm().max_tokens >= TOKEN_MULT.M ? 'M' : 'K'
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
    max_tokens: row.max_tokens ?? 4096,
    priority: row.priority ?? 0,
    enabled: row.enabled,
  })
  modelBare.value = bareOf(row.model ?? '')
  platformKey.value = matchPlatformKey()
  fitTokenUnit(row.max_tokens ?? 4096)
  dialogVisible.value = true
}

async function onSave() {
  // 新建时 api_key 必填
  if (!isEdit.value && !form.api_key) {
    ElMessage.warning(t('models.apiKeyRequired'))
    return
  }
  // 用「数值 + 单位」换算成原生 token 数；模型名拼上前缀成复合名，一并落库
  form.max_tokens = tokenTotal.value
  form.model = resolvedComposite.value
  saving.value = true
  try {
    if (isEdit.value && editingId.value != null) {
      // 若仍是占位 ****** 则原样提交，后端按「不修改」处理
      await updateModel(editingId.value, { ...form })
    } else {
      await createModel({ ...form })
    }
    ElMessage.success(t('common.saved'))
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
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
    ElMessage.success(t('models.testOk', { name: row.name }))
  } catch (e: any) {
    const msg = e?.response?.data?.detail || t('models.testFailed')
    ElMessage.error(msg)
  }
}

async function onDelete(row: ModelItem) {
  await ElMessageBox.confirm(t('models.deleteConfirm', { name: row.name }), t('common.tip'), {
    type: 'warning',
  })
  await deleteModel(row.id)
  ElMessage.success(t('common.deleted'))
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
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.token-editor {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}
.token-eq {
  margin: 0;
  white-space: nowrap;
}
.token-presets {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.token-chip {
  cursor: pointer;
}
.cap-card {
  margin-top: 16px;
}
.chain-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.chain-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  white-space: nowrap;
  width: 64px;
}
.fallback-tag {
  margin-left: 0;
}
.rule-note {
  font-size: 12px;
  margin-top: 10px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-left: 6px;
}
</style>