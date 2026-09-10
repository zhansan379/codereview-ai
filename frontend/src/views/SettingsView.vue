<template>
  <div>
    <el-card>
      <template #header>{{ $t('settings.forgeTitle') }}</template>
      <p class="intro">
        <i18n-t keypath="settings.forgeIntro" scope="global">
          <template #githubEnv><code>CR_GITHUB_TOKEN</code></template>
          <template #gitlabEnv><code>CR_GITLAB_TOKEN</code></template>
        </i18n-t>
      </p>

      <div v-for="prov in providers" :key="prov" class="forge-card">
        <h3>{{ provLabels[prov] }}</h3>
        <el-form label-width="auto">
          <el-form-item label="URL">
            <el-input v-model="form[prov].url" :placeholder="defaults[prov]" />
            <div class="form-tip">{{ $t('settings.urlTip', { url: defaults[prov] }) }}</div>
          </el-form-item>
          <el-form-item label="Token">
            <el-input
              v-model="form[prov].token"
              type="password"
              show-password
              :placeholder="envActive[prov] ? $t('settings.tokenFromEnv') : $t('settings.tokenPlaceholder')"
            />
            <div class="form-tip" v-if="envActive[prov]">
              <i18n-t keypath="settings.envTokenTip" scope="global">
                <template #env><code>CR_{{ prov.toUpperCase() }}_TOKEN</code></template>
              </i18n-t>
            </div>
            <div class="form-tip" v-else>
              {{ $t('settings.dbTokenTip') }}
            </div>
          </el-form-item>
        </el-form>
        <el-row :gutter="8" class="test-row">
          <el-button :loading="testing[prov]" @click="onTest(prov)">{{ $t('settings.testConnection') }}</el-button>
        </el-row>

        <div v-if="caps[prov]" class="capability-matrix">
          <div class="capability-title">
            {{ $t('settings.capabilityTitle') }}<span class="capability-hint">{{ $t('settings.capabilityHint') }}</span>
          </div>
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
          <span class="form-tip" style="margin-left: 8px">{{ $t('settings.concurrencyTip') }}</span>
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
        <i18n-t keypath="settings.autoIntro" scope="global">
          <template #independent><strong>{{ $t('settings.autoIndependent') }}</strong></template>
          <template #priority><strong>{{ $t('settings.autoPriority') }}</strong></template>
        </i18n-t>
      </p>
      <el-form label-width="auto">
        <el-form-item :label="$t('settings.pushTrack')">
          <el-switch v-model="pushEnabled" />
          <span class="form-tip" style="margin-left: 8px">
            {{ $t('settings.pushTip') }}
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
          <span class="form-tip" style="margin-left: 8px">
            {{ $t('settings.mrTip') }}
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

    <el-card class="note-card">
      <template #header>{{ $t('settings.securityTitle') }}</template>
      <el-descriptions :column="1" border>
        <el-descriptions-item :label="$t('settings.webhookSecretLabel')">
          {{ $t('settings.webhookSecretText') }}
        </el-descriptions-item>
        <el-descriptions-item :label="$t('settings.jwtLabel')">
          {{ $t('settings.jwtText') }}
        </el-descriptions-item>
        <el-descriptions-item :label="$t('settings.tokenStoreLabel')">
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
import { listForges, updateForge, testForge, getConcurrency, setConcurrency, getPushReviewDefault, setPushReviewDefault, getMrReviewDefault, setMrReviewDefault } from '../api'
import type { ForgeCapability } from '../api'

const { t } = useI18n()

const MASK = '******'
const providers = ['github', 'gitlab'] as const
const defaults: Record<string, string> = {
  github: 'https://api.github.com',
  gitlab: 'https://gitlab.com',
}
const provLabels: Record<string, string> = { github: 'GitHub', gitlab: 'GitLab' }

interface ForgeForm {
  url: string
  token: string
  enabled: boolean
}
const emptyForm = (): ForgeForm => ({ url: '', token: '', enabled: true })
const form: Record<'github' | 'gitlab', ForgeForm> = reactive({
  github: emptyForm(),
  gitlab: emptyForm(),
})
const envActive = reactive({ github: false, gitlab: false })
const testing = reactive({ github: false, gitlab: false })
const caps = reactive<Record<string, ForgeCapability[] | undefined>>({ github: undefined, gitlab: undefined })
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

async function onTest(provider: 'github' | 'gitlab') {
  testing[provider] = true
  try {
    const f = form[provider]
    // token 为真实新值 → 带上传测；为 ******/空 → 交给后端按当前有效配置（env/DB）解析
    const body = f.token && f.token !== MASK ? { url: f.url, token: f.token } : {}
    const result = await testForge(provider, body)
    caps[provider] = result.capabilities
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
.save-bar {
  margin-top: 4px;
}
.test-row {
  margin-top: 12px;
}
.capability-matrix {
  margin-top: 12px;
}
.capability-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 6px;
}
.capability-hint {
  font-weight: 400;
  color: var(--el-text-color-secondary);
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