<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button v-if="auth.hasPerm('projects:manage')" type="primary" @click="openCreate">新增项目</el-button>
        <el-button v-if="auth.hasPerm('pulls:manage')" :loading="pollBusy" @click="onPoll">
          {{ pollBusy ? '补拉中…' : '补拉 PR/MR' }}
        </el-button>
      </div>
      <div v-if="pollBusy" class="poll-progress">
        <span class="spinner" /> <template v-if="pollProgress && pollProgress.total > 0">
          补拉进行中 {{ pollProgress.done }}/{{ pollProgress.total }}（新 {{ pollProgress.new }}，跳过 {{ pollProgress.skipped }}）…
        </template>
        <template v-else>补拉进行中…</template>
        正在入队打开 PR/MR 的审查，可切换页面，审查在后台进行
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="repo_full_name" label="仓库" min-width="180" />
        <el-table-column prop="provider" label="平台" width="100" />
        <el-table-column prop="web_url" label="Web URL" min-width="160" show-overflow-tooltip />
        <el-table-column prop="review_strategy" label="审查策略" width="120" />
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button v-if="auth.hasPerm('projects:manage')" link type="primary" @click="openMembers(row)">成员</el-button>
            <el-button v-if="auth.hasPerm('projects:manage')" link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button v-if="auth.hasPerm('projects:manage')" link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑项目' : '新增项目'" width="860px">
      <el-form :model="form" label-width="110px">
        <!-- 矮字段两两一行压缩弹窗高度（A 方案：双列网格） -->
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="平台" required>
              <el-select v-model="form.provider" style="width: 100%">
                <el-option label="GitHub" value="github" />
                <el-option label="GitLab" value="gitlab" />
                <el-option label="Gitea" value="gitea" />
                <el-option label="Gitee" value="gitee" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="审查策略">
              <el-select v-model="form.review_strategy" style="width: 100%">
                <el-option label="diff（普通 diff 分组审查）" value="diff" />
                <el-option label="agentic（沙箱探索式，需额外配置）" value="agentic" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="仓库 ID" required>
              <el-input v-model="form.repo_id" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="仓库全名" required>
              <el-input v-model="form.repo_full_name" placeholder="owner/repo" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="Web URL">
              <el-input
                v-model="form.web_url"
                placeholder="粘贴仓库链接后点「解析」，自动回填 仓库ID / 仓库全名"
              >
                <template #append>
                  <el-button :loading="resolving" @click="onResolve">解析</el-button>
                </template>
              </el-input>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="分支规则">
              <el-input v-model="form.branch_rule" placeholder="如 main" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="Push 分支规则">
              <el-input
                v-model="form.push_branch_globs"
                placeholder="如 main,release/*；留空继承全局"
              />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="Push 审查">
              <el-radio-group v-model="form.push_mode">
                <el-radio label="on">开启</el-radio>
                <el-radio label="off">关闭</el-radio>
                <el-radio label="inherit">跟随全局</el-radio>
              </el-radio-group>
              <div class="field-hint">跟随全局 = 交全局默认层裁决：「设置」页「自动审查触发」开关落库值优先，无落库行才回落到环境变量 CR_PUSH_REVIEW_ENABLED。</div>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="MR 审查">
              <el-radio-group v-model="form.mr_mode">
                <el-radio label="on">开启</el-radio>
                <el-radio label="off">关闭</el-radio>
                <el-radio label="inherit">跟随全局</el-radio>
              </el-radio-group>
              <div class="field-hint">MR 到达是否自动审；跟随全局 = 交全局默认层裁决：「设置」页「自动审查触发」MR 轨开关落库值优先，无落库行才回落到环境变量 CR_MR_REVIEW_ENABLED。</div>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="文件扩展名">
              <el-select
                v-model="form.file_extensions"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="输入后回车添加，如 .py"
                style="width: 100%"
              />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="Prompt 后缀">
              <el-input v-model="form.prompt_suffix" type="textarea" :rows="2" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="得分阈值">
              <el-input-number v-model="form.score_threshold" :min="0" :max="100" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="仓库启用">
              <el-switch v-model="form.enabled" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="阻塞合并">
              <el-switch v-model="form.enforce_score_threshold" />
              <span class="field-hint" style="margin-left: 8px">开：总分低于阈值时对 head commit 发失败状态（阻塞合并）
                <el-tooltip placement="top" :show-after="50">
                  <template #content>
                    分平台效果：<br/>· GitHub 写 commit status「failure」(context codereview-ai)，分支保护要求该检查通过才真正阻塞合并<br/>· GitLab 写 commit status「failed」，合并检查「Pipeline must succeed」开启时才阻塞<br/>· Gitea / Gitee 暂未实现该状态回写，开启无效果
                  </template>
                  <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
                </el-tooltip>
              </span>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>

    <!-- 项目成员（F5.11 项目级隔离） -->
    <el-dialog v-model="memberVisible" :title="`成员：${memberProject?.repo_full_name || memberProject?.id || ''}`" width="520px">
      <el-form label-width="90px">
        <el-form-item label="成员">
          <el-select
            v-model="memberUserIds"
            multiple
            filterable
            collapse-tags
            style="width: 100%"
            placeholder="选择可访问该项目的用户"
          >
            <el-option
              v-for="u in allUsers"
              :key="u.id"
              :label="`${u.username}${u.display_name ? '（' + u.display_name + '）' : ''}`"
              :value="u.id"
            />
          </el-select>
          <div class="form-tip">项目级权限经成员关系生效；全项目角色无需在此勾选即可看所有项目。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="memberVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSaveMembers">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listProjects,
  createProject,
  updateProject,
  deleteProject,
  resolveRepo,
  listProjectMembers,
  setProjectMembers,
  listUsers,
  type Project,
  type UserRow,
} from '../api'
import { pollBusy, pollProgress, triggerPoll, resumePollWatchIfBusy } from './usePoll'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()

const items = ref<Project[]>([])
const loading = ref(false)
const saving = ref(false)
const resolving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const memberVisible = ref(false)
const memberProject = ref<Project | null>(null)
const memberUserIds = ref<number[]>([])
const allUsers = ref<UserRow[]>([])
const editingId = ref<number | null>(null)

const emptyForm = () => ({
  provider: '',
  repo_id: '',
  repo_full_name: '',
  web_url: '',
  branch_rule: '',
  file_extensions: [] as string[],
  review_strategy: 'diff',
  prompt_suffix: '',
  score_threshold: 80,
  enforce_score_threshold: false,
  enabled: true,
  // push 审查（三态）：on=开启 / off=关闭 / inherit=跟随全局默认（DB 落库值优先，env 兜底）
  push_mode: 'inherit' as 'on' | 'off' | 'inherit',
  push_branch_globs: '',
  // MR 审查（三态，与 push 对称）：on/off/inherit=跟随全局默认
  mr_mode: 'inherit' as 'on' | 'off' | 'inherit',
})
const form = reactive(emptyForm())

async function load() {
  loading.value = true
  try {
    items.value = await listProjects()
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
function openEdit(row: Project) {
  isEdit.value = true
  editingId.value = row.id
  Object.assign(form, {
    provider: row.provider,
    repo_id: row.repo_id,
    repo_full_name: row.repo_full_name,
    web_url: row.web_url || '',
    branch_rule: row.branch_rule || '',
    file_extensions: row.file_extensions ? row.file_extensions.split(',').map((s) => s.trim()).filter(Boolean) : [],
    review_strategy: row.review_strategy || '',
    prompt_suffix: row.prompt_suffix || '',
    score_threshold: row.score_threshold ?? 80,
    enforce_score_threshold: row.enforce_score_threshold ?? false,
    enabled: row.enabled,
    push_mode: row.push_enabled === true ? 'on' : row.push_enabled === false ? 'off' : 'inherit',
    push_branch_globs: row.push_branch_globs || '',
    mr_mode: row.mr_enabled === true ? 'on' : row.mr_enabled === false ? 'off' : 'inherit',
  })
  dialogVisible.value = true
}

async function onSave() {
  saving.value = true
  try {
    const payload = {
      ...form,
      push_mode: undefined,
      mr_mode: undefined,
      file_extensions: form.file_extensions.join(',').replace(/,\s*/g, ','),
      push_enabled: form.push_mode === 'inherit' ? null : form.push_mode === 'on',
      mr_enabled: form.mr_mode === 'inherit' ? null : form.mr_mode === 'on',
    // push_branch_globs 已含在展开的 form 里
    }
    if (isEdit.value && editingId.value != null) {
      await updateProject(editingId.value, payload)
    } else {
      await createProject(payload)
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

async function onPoll() {
  await triggerPoll()
}

// 从仓库链接本地解析 "owner/repo"（Gitea/Gitee 用；GitHub 语义同，但走后端统一处理）
function parseOwnerRepo(url: string): string {
  try {
    const u = new URL(url.trim())
    const segs = u.pathname.split('/').filter(Boolean)
    if (segs.length >= 2) {
      const repo = segs[1].replace(/\.git$/, '')
      return `${segs[0]}/${repo}`
    }
  } catch {
    /* ignore */
  }
  return ''
}

async function onResolve() {
  const url = form.web_url.trim()
  if (!form.provider) return ElMessage.warning('请先选择平台')
  if (!url) return ElMessage.warning('请先填入仓库链接')
  resolving.value = true
  try {
    if (form.provider === 'github' || form.provider === 'gitlab') {
      const meta = await resolveRepo({ provider: form.provider, url })
      form.repo_id = meta.repo_id
      form.repo_full_name = meta.repo_full_name
      if (meta.web_url) form.web_url = meta.web_url
      ElMessage.success('解析成功')
    } else {
      // Gitea / Gitee：平台暂无后端解析，本地取 owner/repo（repo_id 同为该路径）
      const path = parseOwnerRepo(url)
      if (!path) return ElMessage.warning('无法从链接解析出仓库')
      form.repo_id = path
      form.repo_full_name = path
      ElMessage.success('解析成功')
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '解析失败')
  } finally {
    resolving.value = false
  }
}

async function onDelete(row: Project) {
  await ElMessageBox.confirm(`确认删除项目「${row.repo_full_name}」？`, '提示', { type: 'warning' })
  await deleteProject(row.id)
  ElMessage.success('已删除')
  load()
}

// ── 项目成员（F5.11 项目级隔离）────────────────────────────────────
async function openMembers(row: Project) {
  memberProject.value = row
  try {
    const mems = await listProjectMembers(row.id)
    memberUserIds.value = mems.map((m) => Number(m.id))
    if (!allUsers.value.length) allUsers.value = await listUsers()
  } catch (e: any) {
    return ElMessage.error(e?.response?.data?.detail || '加载成员失败')
  }
  memberVisible.value = true
}
async function onSaveMembers() {
  saving.value = true
  try {
    await setProjectMembers(memberProject.value!.id, memberUserIds.value)
    ElMessage.success('成员已更新')
    memberVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  load()
  resumePollWatchIfBusy() // 若补拉仍在后台跑（切页回来）→ 恢复在途显示
})
</script>

<style scoped>
.toolbar {
  margin-bottom: 12px;
  align-items: center;
}
.poll-hint {
  margin-left: 12px;
  font-size: 12px;
  color: #909399;
}
.poll-progress {
  margin-bottom: 12px;
  padding: 8px 12px;
  border: 1px solid #e1e8f0;
  border-radius: 6px;
  background: #f5f8fc;
  font-size: 13px;
  color: #4a5b6d;
}
.spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  margin-right: 6px;
  vertical-align: -1px;
  border: 2px solid #409eff;
  border-right-color: transparent;
  border-radius: 50%;
  animation: poll-spin 0.8s linear infinite;
}
@keyframes poll-spin {
  to {
    transform: rotate(360deg);
  }
}
.field-hint {
  margin-left: 8px;
  font-size: 12px;
  color: #909399;
}
</style>