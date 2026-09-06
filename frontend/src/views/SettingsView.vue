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
        <el-button :loading="testing[prov]" @click="onTest(prov)">测试连接</el-button>
      </div>

      <div class="save-bar">
        <el-button type="primary" :loading="saving" @click="onSave">保存配置</el-button>
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
import { listForges, updateForge, testForge } from '../api'

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
const saving = ref(false)

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
    await testForge(provider, body)
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

onMounted(load)
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
.note-card {
  margin-top: 16px;
}
</style>