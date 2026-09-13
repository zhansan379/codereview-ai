<template>
  <div>
    <el-card data-tour="settings-forge">
      <template #header>{{ $t('settings.forgeTitle') }}</template>
      <p class="intro">
        {{ $t('settings.forgeIntro') }}
        <el-tooltip placement="top" :show-after="50">
          <template #content>
            {{ $t('settings.forgeTip.token') }}<br/>{{ $t('settings.forgeTip.env') }}
          </template>
          <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
        </el-tooltip>
      </p>

      <div v-for="prov in providers" :key="prov" class="forge-card">
        <h3>{{ provLabels[prov] }}</h3>
        <el-form label-width="auto">
          <el-form-item label="URL">
            <el-input v-model="form[prov].url" :placeholder="defaults[prov]" />
            <span class="field-hint">{{ $t('settings.urlHint') }}
              <el-tooltip placement="top" :show-after="50">
                <template #content>{{ $t('settings.urlTip', { url: defaults[prov] }) }}</template>
                <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
              </el-tooltip>
            </span>
          </el-form-item>
          <el-form-item label="Token">
            <el-input
              v-model="form[prov].token"
              type="password"
              show-password
              :placeholder="envActive[prov] ? $t('settings.tokenFromEnv') : $t('settings.tokenPlaceholder')"
            />
            <span class="field-hint" v-if="envActive[prov]">{{ $t('settings.tokenEnvHint') }}
              <el-tooltip placement="top" :show-after="50">
                <template #content>
                  {{ $t('settings.envTokenTip', { env: `CR_${prov.toUpperCase()}_TOKEN` }) }}<br/>{{ $t('settings.envTokenFallback') }}
                </template>
                <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
              </el-tooltip>
            </span>
            <span class="field-hint" v-else>{{ $t('settings.tokenDbHint') }}
              <el-tooltip placement="top" :show-after="50">
                <template #content>{{ $t('settings.dbTokenTip') }}</template>
                <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
              </el-tooltip>
            </span>
          </el-form-item>
        </el-form>
        <el-row :gutter="8" class="test-row">
          <el-button :loading="testing[prov]" @click="onTest(prov)">{{ $t('settings.testConnection') }}</el-button>
        </el-row>

        <div v-if="caps[prov]" class="capability-matrix">
          <div class="capability-title">
            {{ $t('settings.capabilityTitle') }}
            <el-tooltip placement="top" :show-after="50">
              <template #content>
                {{ $t('settings.capabilityTip.what') }}<br/>{{ $t('settings.capabilityTip.missing') }}
              </template>
              <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </div>
          <el-alert
            v-if="capSources[prov] === 'env'"
            type="warning"
            :closable="false"
            show-icon
            class="capability-alert"
            :title="$t('settings.capTokenEnvWarn', { env: `CR_${prov.toUpperCase()}_TOKEN` })"
          />
          <el-table :data="caps[prov]" size="small" border>
            <el-table-column prop="label" :label="$t('settings.capabilityCol')" min-width="160" />
            <el-table-column :label="$t('common.status')" width="110">
              <template #default="{ row }">
                <el-tag
                  :type="row.status === 'ok' ? 'success' : row.status === 'missing' ? 'danger' : 'info'"
                  disable-transitions
                >
                  {{ row.status === 'ok' ? $t('settings.capOk') : row.status === 'missing' ? $t('settings.capMissing') : $t('common.unknown') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="detail" :label="$t('settings.capabilityDetailCol')" min-width="200">
              <template #default="{ row }">
                <span class="capability-detail">{{ row.detail || '—' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>

      <div class="save-bar">
        <el-button type="primary" :loading="saving" @click="onSave">{{ $t('settings.saveForge') }}</el-button>
      </div>
    </el-card>

    <el-card class="concurrency-card">
      <template #header>{{ $t('settings.concurrencyTitle') }}</template>
      <p class="intro">
        {{ $t('settings.concurrencyIntro') }}
      </p>
      <el-form label-width="auto">
        <el-form-item :label="$t('settings.concurrencyLimit')">
          <el-input-number v-model="concurrency" :min="1" :max="32" />
          <span class="field-hint" style="margin-left: 8px">{{ $t('settings.concurrencyHint') }}
            <el-tooltip placement="top" :show-after="50">
              <template #content>
                {{ $t('settings.concurrencyTip.what') }}<br/>{{ $t('settings.concurrencyTip.more') }}
              </template>
              <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </span>
        </el-form-item>
      </el-form>
      <div class="save-bar">
        <el-button type="primary" :loading="ccSaving" @click="onSaveConcurrency">{{ $t('settings.saveConcurrency') }}</el-button>
        <span v-if="ccActive" class="form-tip">{{ $t('settings.concurrencyActive', { n: concurrency }) }}</span>
        <span v-else class="hint">{{ $t('settings.runnerIdle') }}</span>
      </div>
    </el-card>

    <el-card class="push-card">
      <template #header>{{ $t('settings.autoTitle') }}</template>
      <p class="intro">
        {{ $t('settings.autoIntro') }}
        <el-tooltip placement="top" :show-after="50">
          <template #content>
            {{ $t('settings.autoTip.independent') }}<br/>{{ $t('settings.autoTip.priority') }}
          </template>
          <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
        </el-tooltip>
      </p>
      <el-form label-width="auto">
        <el-form-item :label="$t('settings.pushTrack')">
          <el-switch v-model="pushEnabled" />
          <span class="field-hint" style="margin-left: 8px">{{ $t('settings.pushHint') }}
            <el-tooltip placement="top" :show-after="50">
              <template #content>
                {{ $t('settings.pushTip.what') }}<br/>{{ $t('settings.pushTip.override') }}
              </template>
              <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </span>
        </el-form-item>
        <el-form-item v-if="pushSource === 'env'" :label="$t('settings.pushSourceLabel')">
          <span class="hint">
            <i18n-t keypath="settings.envSourceHint" scope="global">
              <template #env><code>CR_PUSH_REVIEW_ENABLED</code></template>
            </i18n-t>
          </span>
        </el-form-item>
        <el-divider class="track-divider" />
        <el-form-item :label="$t('settings.mrTrack')">
          <el-switch v-model="mrEnabled" />
          <span class="field-hint" style="margin-left: 8px">{{ $t('settings.mrHint') }}
            <el-tooltip placement="top" :show-after="50">
              <template #content>
                {{ $t('settings.mrTip.what') }}<br/>{{ $t('settings.mrTip.override') }}
              </template>
              <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </span>
        </el-form-item>
        <el-form-item v-if="mrSource === 'env'" :label="$t('settings.mrSourceLabel')">
          <span class="hint">
            <i18n-t keypath="settings.envSourceHint" scope="global">
              <template #env><code>CR_MR_REVIEW_ENABLED</code></template>
            </i18n-t>
          </span>
        </el-form-item>
      </el-form>
      <div class="save-bar">
        <el-button :loading="pushSaving" @click="onSavePushDefault">{{ $t('settings.savePushTrack') }}</el-button>
        <el-button :loading="mrSaving" @click="onSaveMrDefault">{{ $t('settings.saveMrTrack') }}</el-button>
      </div>
    </el-card>

    <el-card class="push-card">
      <template #header>{{ $t('settings.pollScopeTitle') }}</template>
      <p class="intro">{{ $t('settings.pollScopeIntro') }}</p>
      <el-form label-width="auto">
        <el-form-item :label="$t('settings.includeClosed')">
          <el-switch v-model="pollIncludeClosed" />
          <span class="field-hint" style="margin-left: 8px">{{ $t('settings.pollScopeHint') }}
            <el-tooltip placement="top" :show-after="50">
              <template #content>
                {{ $t('settings.pollScopeTip.on') }}<br/>{{ $t('settings.pollScopeTip.off') }}
              </template>
              <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </span>
        </el-form-item>
        <el-form-item v-if="pollScopeSource === 'env'" :label="$t('settings.pollScopeSourceLabel')">
          <span class="hint">
            <i18n-t keypath="settings.envSourceHint" scope="global">
              <template #env><code>CR_POLL_INCLUDE_CLOSED</code></template>
            </i18n-t>
          </span>
        </el-form-item>
      </el-form>
      <div class="save-bar">
        <el-button type="primary" :loading="pollScopeSaving" @click="onSavePollScope">{{ $t('settings.savePollScope') }}</el-button>
      </div>
    </el-card>

    <el-card class="note-card">
      <template #header>{{ $t('settings.securityTitle') }}</template>
      <el-descriptions :column="1" border>
        <el-descriptions-item>
          <template #label>
            {{ $t('settings.webhookSecretLabel') }}
            <el-tooltip placement="top" :show-after="50">
              <template #content>
                {{ $t('settings.webhookSecretTip.what') }}<br/>{{ $t('settings.webhookSecretTip.why') }}
              </template>
              <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          {{ $t('settings.webhookSecretText') }}
        </el-descriptions-item>
        <el-descriptions-item>
          <template #label>
            {{ $t('settings.jwtLabel') }}
            <el-tooltip placement="top" :show-after="50">
              <template #content>
                {{ $t('settings.jwtTip.what') }}<br/>{{ $t('settings.jwtTip.expire') }}
              </template>
              <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          {{ $t('settings.jwtText') }}
        </el-descriptions-item>
        <el-descriptions-item>
          <template #label>
            {{ $t('settings.tokenStoreLabel') }}
            <el-tooltip placement="top" :show-after="50">
              <template #content>
                {{ $t('settings.tokenStoreTip.what') }}<br/>{{ $t('settings.tokenStoreTip.gone') }}
              </template>
              <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          {{ $t('settings.tokenStoreText') }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { listForges, updateForge, testForge, getConcurrency, setConcurrency, getPushReviewDefault, setPushReviewDefault, getMrReviewDefault, setMrReviewDefault, getPollIncludeClosed, setPollIncludeClosed } from '../api'
import type { ForgeCapability } from '../api'

const { t } = useI18n()

const MASK = '******'
const providers = ['github', 'gitlab', 'gitee'] as const
const defaults: Record<string, string> = {
  github: 'https://api.github.com',
  gitlab: 'https://gitlab.com',
  gitee: 'https://gitee.com/api/v5',
}
const provLabels: Record<string, string> = { github: 'GitHub', gitlab: 'GitLab', gitee: 'Gitee' }

interface ForgeForm {
  url: string
  token: string
  enabled: boolean
}
const emptyForm = (): ForgeForm => ({ url: '', token: '', enabled: true })
const form: Record<string, ForgeForm> = reactive({
  github: emptyForm(),
  gitlab: emptyForm(),
  gitee: emptyForm(),
})
const envActive = reactive<Record<string, boolean>>({ github: false, gitlab: false, gitee: false })
const testing = reactive<Record<string, boolean>>({ github: false, gitlab: false, gitee: false })
const caps = reactive<Record<string, ForgeCapability[] | undefined>>({ github: undefined, gitlab: undefined, gitee: undefined })
const capSources = reactive<Record<string, 'env' | 'db' | 'manual' | '' | undefined>>({
  github: undefined,
  gitlab: undefined,
  gitee: undefined,
})
const saving = ref(false)

// —— 审查并发 ——
const concurrency = ref(4)
const ccActive = ref(true)
const ccSaving = ref(false)

// —— push / MR 自动审查默认开关（§7.7 双轨各独立全局默认）——
const pushEnabled = ref(false)
const pushSource = ref<'db' | 'env'>('db')
const pushSaving = ref(false)

// —— MR 轨（与 push 对称）——
const mrEnabled = ref(false)
const mrSource = ref<'db' | 'env'>('db')
const mrSaving = ref(false)

// —— 补拉范围（§9：是否同时拉取已关闭/已合并的 PR/MR）——
const pollIncludeClosed = ref(false)
const pollScopeSource = ref<'db' | 'env'>('db')
const pollScopeSaving = ref(false)

async function loadPollScope() {
  try {
    const s = await getPollIncludeClosed()
    pollIncludeClosed.value = s.enabled
    pollScopeSource.value = s.source
  } catch {
    /* 后端未暴露该接口时（旧版）静默跳过，不阻塞平台页加载 */
  }
}

async function onSavePollScope() {
  pollScopeSaving.value = true
  try {
    const s = await setPollIncludeClosed({ enabled: pollIncludeClosed.value })
    pollIncludeClosed.value = s.enabled
    pollScopeSource.value = s.source
    ElMessage.success(
      s.enabled ? t('settings.pollScopeOn') : t('settings.pollScopeOff'),
    )
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    pollScopeSaving.value = false
  }
}

async function loadMrDefault() {
  try {
    const s = await getMrReviewDefault()
    mrEnabled.value = s.enabled
    mrSource.value = s.source
  } catch {
    /* 后端未暴露该接口时（旧版）静默跳过，不阻塞平台页加载 */
  }
}

async function onSaveMrDefault() {
  mrSaving.value = true
  try {
    const s = await setMrReviewDefault({ enabled: mrEnabled.value })
    mrEnabled.value = s.enabled
    mrSource.value = s.source
    ElMessage.success(s.enabled ? t('settings.mrAutoOn') : t('settings.mrAutoOff'))
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    mrSaving.value = false
  }
}

async function loadPushDefault() {
  try {
    const s = await getPushReviewDefault()
    pushEnabled.value = s.enabled
    pushSource.value = s.source
  } catch {
    /* 后端未暴露该接口时（旧版）静默跳过，不阻塞平台页加载 */
  }
}

async function onSavePushDefault() {
  pushSaving.value = true
  try {
    const s = await setPushReviewDefault({ enabled: pushEnabled.value })
    pushEnabled.value = s.enabled
    pushSource.value = s.source
    ElMessage.success(s.enabled ? t('settings.pushAutoOn') : t('settings.pushAutoOff'))
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    pushSaving.value = false
  }
}

async function loadConcurrency() {
  try {
    const s = await getConcurrency()
    concurrency.value = s.concurrency
    ccActive.value = s.active
  } catch {
    /* 后端未暴露该接口时（旧版）静默跳过，不阻塞平台页加载 */
  }
}

async function onSaveConcurrency() {
  ccSaving.value = true
  try {
    const s = await setConcurrency({ concurrency: Math.max(1, Math.min(32, concurrency.value)) })
    concurrency.value = s.concurrency
    ccActive.value = s.active
    ElMessage.success(
      s.applied ? t('settings.concurrencySaved', { n: s.concurrency }) : t('settings.concurrencySavedPending'),
    )
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    ccSaving.value = false
  }
}

async function load() {
  const items = await listForges()
  for (const p of providers) {
    const it = items.find((x) => x.provider === p)
    form[p].url = it?.url || defaults[p]
    form[p].token = it?.token || ''
    envActive[p] = !!it?.env_active
  }
}

async function onTest(provider: string) {
  testing[provider] = true
  try {
    const f = form[provider]
    // token 为真实新值 → 带上传测；为 ******/空 → 交给后端按当前有效配置（env/DB）解析
    const body = f.token && f.token !== MASK ? { url: f.url, token: f.token } : {}
    const result = await testForge(provider, body)
    caps[provider] = result.capabilities
    capSources[provider] = result.token_source || ''
    ElMessage.success(t('settings.testOk', { name: provLabels[provider] }))
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('settings.testFailed'))
  } finally {
    testing[provider] = false
  }
}

async function onSave() {
  saving.value = true
  try {
    for (const p of providers) {
      await updateForge(p, { url: form[p].url, token: form[p].token, enabled: true })
    }
    ElMessage.success(t('settings.forgeSaved'))
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  load()
  loadConcurrency()
  loadPushDefault()
  loadMrDefault()
  loadPollScope()
})
</script>

<style scoped>
.intro {
  color: var(--el-text-color-regular);
  font-size: 13px;
  margin: 0 0 12px;
  line-height: 1.6;
}
.forge-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 12px 16px 16px;
  margin-bottom: 16px;
}
.forge-card h3 {
  margin: 0 0 12px;
  font-size: 15px;
  color: var(--el-text-color-primary);
}
.form-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.field-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.save-bar {
  margin-top: 4px;
}
.test-row {
  margin-top: 12px;
}
.capability-matrix {
  margin-top: 12px;
}
.capability-alert {
  margin-bottom: 8px;
}
.capability-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 6px;
}
.capability-detail {
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.concurrency-card,
.push-card,
.note-card {
  margin-top: 16px;
}
.hint {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-color-warning);
}
</style>