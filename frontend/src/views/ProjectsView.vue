<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button v-if="auth.hasPerm('projects:manage')" type="primary" @click="openCreate">{{ $t('projects.createBtn') }}</el-button>
        <el-button v-if="auth.hasPerm('pulls:manage')" :loading="pollBusy" @click="onPoll">
          {{ pollBusy ? $t('projects.pollBtnBusy') : $t('projects.pollBtn') }}
        </el-button>
      </div>
      <div v-if="pollBusy" class="poll-progress">
        <span class="spinner" /> <template v-if="pollProgress && pollProgress.total > 0">
          {{
            $t('projects.pollProgress', {
              done: pollProgress.done,
              total: pollProgress.total,
              added: pollProgress.new,
              skipped: pollProgress.skipped,
            })
          }}
        </template>
        <template v-else>{{ $t('projects.pollRunning') }}</template>
        {{ $t('projects.pollHint') }}
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" :label="$t('common.id')" width="70" />
        <el-table-column prop="repo_full_name" :label="$t('projects.columns.repo')" min-width="180" />
        <el-table-column prop="provider" :label="$t('projects.columns.provider')" width="100" />
        <el-table-column prop="web_url" label="Web URL" min-width="160" show-overflow-tooltip />
        <el-table-column prop="review_strategy" :label="$t('projects.columns.reviewStrategy')" :width="colWidth(120)" />
        <el-table-column :label="$t('projects.columns.enabled')" :width="colWidth(80)">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? $t('common.yes') : $t('common.no') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.actions')" width="220" fixed="right">
          <template #default="{ row }">
            <el-button v-if="auth.hasPerm('projects:manage')" link type="primary" @click="openMembers(row)">{{ $t('projects.membersBtn') }}</el-button>
            <el-button v-if="auth.hasPerm('projects:manage')" link type="primary" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button v-if="auth.hasPerm('projects:manage')" link type="danger" @click="onDelete(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? $t('projects.dialog.editTitle') : $t('projects.dialog.createTitle')" width="860px">
      <el-form :model="form" label-width="auto">
        <!-- 矮字段两两一行压缩弹窗高度（A 方案：双列网格） -->
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item :label="$t('projects.form.provider')" required>
              <el-select v-model="form.provider" style="width: 100%">
                <el-option label="GitHub" value="github" />
                <el-option label="GitLab" value="gitlab" />
                <el-option label="Gitea" value="gitea" />
                <el-option label="Gitee" value="gitee" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="$t('projects.form.reviewStrategy')">
              <el-select v-model="form.review_strategy" style="width: 100%">
                <el-option :label="$t('projects.form.strategyDiff')" value="diff" />
                <el-option :label="$t('projects.form.strategyAgentic')" value="agentic" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="$t('projects.form.repoId')" required>
              <el-input v-model="form.repo_id" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="$t('projects.form.repoFullName')" required>
              <el-input v-model="form.repo_full_name" placeholder="owner/repo" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item :label="$t('projects.form.webUrl')">
              <el-input
                v-model="form.web_url"
                :placeholder="$t('projects.form.webUrlPlaceholder')"
              >
                <template #append>
                  <el-button :loading="resolving" @click="onResolve">{{ $t('projects.form.resolve') }}</el-button>
                </template>
              </el-input>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="$t('projects.form.branchRule')">
              <el-input v-model="form.branch_rule" :placeholder="$t('projects.form.branchRulePlaceholder')" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="$t('projects.form.pushBranchGlobs')">
              <el-input
                v-model="form.push_branch_globs"
                :placeholder="$t('projects.form.pushBranchGlobsPlaceholder')"
              />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item :label="$t('projects.form.pushReview')">
              <el-radio-group v-model="form.push_mode">
                <el-radio label="on">{{ $t('projects.form.modeOn') }}</el-radio>
                <el-radio label="off">{{ $t('projects.form.modeOff') }}</el-radio>
                <el-radio label="inherit">{{ $t('projects.form.modeInherit') }}</el-radio>
              </el-radio-group>
              <div class="field-hint">{{ $t('projects.form.pushHint') }}</div>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item :label="$t('projects.form.mrReview')">
              <el-radio-group v-model="form.mr_mode">
                <el-radio label="on">{{ $t('projects.form.modeOn') }}</el-radio>
                <el-radio label="off">{{ $t('projects.form.modeOff') }}</el-radio>
                <el-radio label="inherit">{{ $t('projects.form.modeInherit') }}</el-radio>
              </el-radio-group>
              <div class="field-hint">{{ $t('projects.form.mrHint') }}</div>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item :label="$t('projects.form.fileExtensions')">
              <el-select
                v-model="form.file_extensions"
                multiple
                filterable
                allow-create
                default-first-option
                :placeholder="$t('projects.form.fileExtensionsPlaceholder')"
                style="width: 100%"
              />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item :label="$t('projects.form.promptSuffix')">
              <el-input v-model="form.prompt_suffix" type="textarea" :rows="2" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="$t('projects.form.scoreThreshold')">
              <el-input-number v-model="form.score_threshold" :min="0" :max="100" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="$t('projects.form.repoEnabled')">
              <el-switch v-model="form.enabled" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item :label="$t('projects.form.blockMerge')">
              <el-switch v-model="form.enforce_score_threshold" />
              <span class="field-hint" style="margin-left: 8px">{{ $t('projects.form.blockMergeHint') }}
                <el-tooltip placement="top" :show-after="50">
                  <template #content>
                    {{ $t('projects.form.blockMergeTip.title') }}<br/>{{ $t('projects.form.blockMergeTip.github') }}<br/>{{ $t('projects.form.blockMergeTip.gitlab') }}<br/>{{ $t('projects.form.blockMergeTip.others') }}
                  </template>
                  <el-icon style="vertical-align: -2px; margin-left: 4px; cursor: help"><QuestionFilled /></el-icon>
                </el-tooltip>
              </span>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 项目成员（F5.11 项目级隔离） -->
    <el-dialog v-model="memberVisible" :title="$t('projects.members.title', { name: memberProject?.repo_full_name || memberProject?.id || '' })" width="520px">
      <el-form label-width="auto">
        <el-form-item :label="$t('projects.members.label')">
          <el-select
            v-model="memberUserIds"
            multiple
            filterable
            collapse-tags
            style="width: 100%"
            :placeholder="$t('projects.members.placeholder')"
          >
            <el-option
              v-for="u in allUsers"
              :key="u.id"
              :label="u.display_name ? $t('projects.members.userLabel', { username: u.username, display: u.display_name }) : u.username"
              :value="u.id"
            />
          </el-select>
          <div class="form-tip">{{ $t('projects.members.tip') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="memberVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="onSaveMembers">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
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
import { colWidth } from '../composables/useLocale'

const auth = useAuthStore()
const { t } = useI18n()

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
    ElMessage.success(t('projects.msg.saveSuccess'))
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
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
  if (!form.provider) return ElMessage.warning(t('projects.msg.pickProviderFirst'))
  if (!url) return ElMessage.warning(t('projects.msg.fillUrlFirst'))
  resolving.value = true
  try {
    if (form.provider === 'github' || form.provider === 'gitlab') {
      const meta = await resolveRepo({ provider: form.provider, url })
      form.repo_id = meta.repo_id
      form.repo_full_name = meta.repo_full_name
      if (meta.web_url) form.web_url = meta.web_url
      ElMessage.success(t('projects.msg.resolveSuccess'))
    } else {
      // Gitea / Gitee：平台暂无后端解析，本地取 owner/repo（repo_id 同为该路径）
      const path = parseOwnerRepo(url)
      if (!path) return ElMessage.warning(t('projects.msg.resolveNoRepo'))
      form.repo_id = path
      form.repo_full_name = path
      ElMessage.success(t('projects.msg.resolveSuccess'))
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('projects.msg.resolveFailed'))
  } finally {
    resolving.value = false
  }
}

async function onDelete(row: Project) {
  await ElMessageBox.confirm(t('projects.msg.deleteConfirm', { name: row.repo_full_name }), t('common.tip'), { type: 'warning' })
  await deleteProject(row.id)
  ElMessage.success(t('common.deleted'))
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
    return ElMessage.error(e?.response?.data?.detail || t('projects.members.loadFailed'))
  }
  memberVisible.value = true
}
async function onSaveMembers() {
  saving.value = true
  try {
    await setProjectMembers(memberProject.value!.id, memberUserIds.value)
    ElMessage.success(t('projects.members.updated'))
    memberVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
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
  color: var(--el-text-color-secondary);
}
.poll-progress {
  margin-bottom: 12px;
  padding: 8px 12px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  margin-right: 6px;
  vertical-align: -1px;
  border: 2px solid var(--el-color-primary);
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
  color: var(--el-text-color-secondary);
}
</style>