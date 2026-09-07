<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">新增项目</el-button>
        <el-button :loading="pollBusy" @click="onPoll">
          {{ pollBusy ? '补拉中…' : '补拉 PR/MR' }}
        </el-button>
      </div>
      <div v-if="pollBusy" class="poll-progress">
        <span class="spinner" /> 补拉进行中… 正在后台审查打开 PR/MR，可切换页面，完成后将弹出结果
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
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑项目' : '新增项目'" width="640px">
      <el-form :model="form" label-width="110px">
        <el-form-item label="平台" required>
          <el-select v-model="form.provider" style="width: 100%">
            <el-option label="GitHub" value="github" />
            <el-option label="GitLab" value="gitlab" />
            <el-option label="Gitea" value="gitea" />
            <el-option label="Gitee" value="gitee" />
          </el-select>
        </el-form-item>
        <el-form-item label="仓库 ID" required>
          <el-input v-model="form.repo_id" />
        </el-form-item>
        <el-form-item label="仓库全名" required>
          <el-input v-model="form.repo_full_name" placeholder="owner/repo" />
        </el-form-item>
        <el-form-item label="Web URL">
          <el-input v-model="form.web_url" />
        </el-form-item>
        <el-form-item label="分支规则">
          <el-input v-model="form.branch_rule" placeholder="如 main" />
        </el-form-item>
        <el-form-item label="Push 审查">
          <el-radio-group v-model="form.push_mode">
            <el-radio label="on">开启</el-radio>
            <el-radio label="off">关闭</el-radio>
            <el-radio label="inherit">跟随全局</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="Push 分支规则">
          <el-input
            v-model="form.push_branch_globs"
            placeholder="逗号分隔 glob，如 main,release/*；留空继承全局"
          />
        </el-form-item>
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
        <el-form-item label="审查策略">
          <el-select v-model="form.review_strategy" style="width: 100%">
            <el-option label="diff（普通 diff 分组审查）" value="diff" />
            <el-option label="agentic（沙箱探索式，需额外配置）" value="agentic" />
          </el-select>
        </el-form-item>
        <el-form-item label="Prompt 后缀">
          <el-input v-model="form.prompt_suffix" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="得分阈值">
          <el-input-number v-model="form.score_threshold" :min="0" :max="100" />
        </el-form-item>
        <el-form-item label="低于阈值阻塞合并">
          <el-switch v-model="form.enforce_score_threshold" />
          <span class="field-hint">开：总分低于阈值时对该 MR 发 failed 状态，阻塞合并</span>
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
import { listProjects, createProject, updateProject, deleteProject, type Project } from '../api'
import { pollBusy, triggerPoll, resumePollWatchIfBusy } from './usePoll'

const items = ref<Project[]>([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
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
  // push 审查（三态）：on=开启 / off=关闭 / inherit=跟随全局 env 默认
  push_mode: 'inherit' as 'on' | 'off' | 'inherit',
  push_branch_globs: '',
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
  })
  dialogVisible.value = true
}

async function onSave() {
  saving.value = true
  try {
    const payload = {
      ...form,
      push_mode: undefined,
      file_extensions: form.file_extensions.join(',').replace(/,\s*/g, ','),
      push_enabled: form.push_mode === 'inherit' ? null : form.push_mode === 'on',
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

async function onDelete(row: Project) {
  await ElMessageBox.confirm(`确认删除项目「${row.repo_full_name}」？`, '提示', { type: 'warning' })
  await deleteProject(row.id)
  ElMessage.success('已删除')
  load()
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