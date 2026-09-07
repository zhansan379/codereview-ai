<template>
  <div>
    <el-card>
      <template #header>平台接入</template>
      <p class="intro">
        配置 GitHub / GitLab 的 Token 与 URL。保存后立即热更生效（无需重启后端）。
        若对应环境变量（<code>CR_GITHUB_TOKEN</code>/<code>CR_GITLAB_TOKEN</code>）已设置，则以环境变量为准。
      </p>

      <div v-for="prov in providers" :key="prov" class="forge-card">
        <h3>{{ provLabels[prov] }}</h3>
        <el-form label-width="70px">
          <el-form-item label="URL">
            <el-input v-model="form[prov].url" :placeholder="defaults[prov]" />
            <div class="form-tip">自托管实例请改成你自己的地址；留空则以默认 {{ defaults[prov] }} 为准。</div>
          </el-form-item>
          <el-form-item label="Token">
            <el-input
              v-model="form[prov].token"
              type="password"
              show-password
              :placeholder="envActive[prov] ? '环境变量已配置（优先）' : '填写平台 Access Token'"
            />
            <div class="form-tip" v-if="envActive[prov]">
              检测到环境变量 <code>CR_{{ prov.toUpperCase() }}_TOKEN</code>，运行时以它为准；此处保存的 Token 作为兜底/备用。
            </div>
            <div class="form-tip" v-else>
              页面保存的 Token 会加密存入服务端数据库，读回显示 ******。
            </div>
          </el-form-item>
        </el-form>
        <el-row :gutter="8" class="test-row">
          <el-button :loading="testing[prov]" @click="onTest(prov)">测试连接</el-button>
        </el-row>

        <div v-if="caps[prov]" class="capability-matrix">
          <div class="capability-title">
            能力矩阵<span class="capability-hint">（本 Token 支持系统哪些能力）</span>
          </div>
          <el-table :data="caps[prov]" size="small" border>
            <el-table-column prop="label" label="能力" min-width="160" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag
                  :type="row.status === 'ok' ? 'success' : row.status === 'missing' ? 'danger' : 'info'"
                  disable-transitions
                >
                  {{ row.status === 'ok' ? '可用' : row.status === 'missing' ? '缺权限' : '未知' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="detail" label="说明" min-width="200">
              <template #default="{ row }">
                <span class="capability-detail">{{ row.detail || '—' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>

      <div class="save-bar">
        <el-button type="primary" :loading="saving" @click="onSave">保存配置</el-button>
      </div>
    </el-card>

    <el-card class="concurrency-card">
      <template #header>审查并发</template>
      <p class="intro">
        同时进行的代码审查条数（1–32，默认 4）。保存后立即热更生效，无需重启后端。
      </p>
      <el-form label-width="110px">
        <el-form-item label="并发上限">
          <el-input-number v-model="concurrency" :min="1" :max="32" />
          <span class="form-tip" style="margin-left: 8px">数值越大并行审查越多，占用 LLM 并发越高。</span>
        </el-form-item>
      </el-form>
      <div class="save-bar">
        <el-button type="primary" :loading="ccSaving" @click="onSaveConcurrency">保存并发</el-button>
        <span v-if="ccActive" class="form-tip">已生效：当前并发 {{ concurrency }} 条在跑。</span>
        <span v-else class="hint">运行器未启动，配置将落库，待运行器就绪后按此值生效。</span>
      </div>
    </el-card>

    <el-card class="note-card">
      <template #header>安全说明</template>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="Webhook 签名密钥">
          由后端环境变量配置，出于安全考虑不在管理后台展示明文。
        </el-descriptions-item>
        <el-descriptions-item label="JWT 有效期">
          登录返回的 access_token 有有效期（expires_in 字段），过期后自动跳转登录页。
        </el-descriptions-item>
        <el-descriptions-item label="Token 存储">
          登录凭证保存在浏览器 sessionStorage 中，关闭页面即失效。
        </el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { listForges, updateForge, testForge, getConcurrency, setConcurrency } from '../api'
import type { ForgeCapability } from '../api'

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
    ElMessage.success(s.applied ? `已热更生效：并发 ${s.concurrency}` : '已保存，运行器就绪后生效')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
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
    ElMessage.success(`「${provLabels[provider]}」连接正常`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || `连接测试失败`)
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
    ElMessage.success('已保存并热更生效')
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  load()
  loadConcurrency()
})
</script>

<style scoped>
.intro {
  color: #606266;
  font-size: 13px;
  margin: 0 0 12px;
  line-height: 1.6;
}
.forge-card {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 12px 16px 16px;
  margin-bottom: 16px;
}
.forge-card h3 {
  margin: 0 0 12px;
  font-size: 15px;
  color: #303133;
}
.form-tip {
  font-size: 12px;
  color: #909399;
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
  color: #303133;
  margin-bottom: 6px;
}
.capability-hint {
  font-weight: 400;
  color: #909399;
}
.capability-detail {
  font-size: 12px;
  color: #606266;
}
.concurrency-card,
.note-card {
  margin-top: 16px;
}
.hint {
  margin-left: 8px;
  font-size: 12px;
  color: #e6a23c;
}
</style>