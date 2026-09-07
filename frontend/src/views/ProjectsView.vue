<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">新增项目</el-button>
        <el-button :loading="polling" @click="onPoll">补拉 PR/MR</el-button>
        <span class="poll-hint">后台按轮询间隔自动补拉，仅审 head 未变过的打开 PR/MR（新 head 才会审）</span>
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
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listProjects, createProject, updateProject, deleteProject, pollPulls, pollPullStatus, type Project } from '../api'

const items = ref<Project[]>([])
const loading = ref(false)
const saving = ref(false)
const polling = ref(false)
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

let pollTimer: ReturnType<typeof setInterval> | null = null
let pollUid = 0 // 组件卸载/重复触发时丢弃过期回调

async function onPoll() {
  polling.value = true
  const uid = ++pollUid
  try {
    await pollPulls() // 后台触发；若已在跑会返回 409
  } catch (e: any) {
    if (e?.response?.status === 409) {
      // 上一轮仍在后台进行 → 继续等它
    } else {
      ElMessage.error(e?.response?.data?.detail || '补拉触发失败')
      polling.value = false
      return
    }
  }
  // 轮询后台状态直到本轮结束，展示最终报告（切页不会中断后台补拉）
  const tick = async () => {
    try {
      const s = await pollPullStatus()
      if (uid !== pollUid) return // 已被新的一轮/卸载取代
      if (!s.running) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
        polling.value = false
        if (s.error) {
          ElMessage.error(`补拉失败：${s.error}`)
        } else if (s.report) {
          const parts = [`扫描 ${s.report.prs} 个打开 PR/MR`, `新审 ${s.report.new}`, `已审过跳过 ${s.report.skipped}`]
          if (s.report.errors.length) parts.push(`失败 ${s.report.errors.length}`)
          ElMessage.success(`补拉完成：${parts.join('，')}`)
          if (s.report.errors.length) console.warn('补拉失败明细', s.report.errors)
        }
      }
    } catch {
      /* 轮询期间瞬时错误：下一拍再试 */
    }
  }
  await tick()
  if (polling.value) pollTimer = setInterval(tick, 2000)
}

onUnmounted(() => {
  pollUid++ // 丢弃过期回调
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
})

async function onDelete(row: Project) {
  await ElMessageBox.confirm(`确认删除项目「${row.repo_full_name}」？`, '提示', { type: 'warning' })
  await deleteProject(row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
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
.field-hint {
  margin-left: 8px;
  font-size: 12px;
  color: #909399;
}
</style>