<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">新增通知渠道</el-button>
        <el-button @click="openMemberManager">@成员管理</el-button>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="渠道" width="120">
          <template #default="{ row }">
            <el-tag>{{ row.channel }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="project_id" label="项目(ID)" width="110">
          <template #default="{ row }">
            {{ row.project_id === null ? '全局' : row.project_id }}
          </template>
        </el-table-column>
        <el-table-column prop="at_threshold" label="@阈值" width="90" />
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="webhook" label="Webhook" min-width="160" show-overflow-tooltip />
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑通知渠道' : '新增通知渠道'" width="560px">
      <el-form :model="form" label-width="110px">
        <el-form-item label="渠道" required>
          <el-select
            v-model="form.channel"
            :disabled="isEdit"
            style="width: 100%"
            @change="onChannelChange"
          >
            <el-option label="钉钉 dingtalk" value="dingtalk" />
            <el-option label="飞书 feishu" value="feishu" />
            <el-option label="企业微信 wecom" value="wecom" />
          </el-select>
        </el-form-item>
        <el-form-item label="Webhook" required>
          <el-input
            v-model="form.webhook"
            type="password"
            show-password
            :placeholder="isEdit ? '留空或填 ****** 表示不修改' : '请输入 Webhook 地址'"
          />
          <div class="form-tip" v-if="isEdit">读回为 ****** 表示保留原值。</div>
        </el-form-item>
        <el-form-item v-if="form.channel !== 'wecom'" label="Secret" required>
          <el-input
            v-model="form.secret"
            type="password"
            show-password
            :placeholder="isEdit ? '留空或填 ****** 表示不修改' : '请输入 Secret'"
          />
          <div class="form-tip" v-if="isEdit">读回为 ****** 表示保留原值。</div>
          <div class="form-tip" v-else>钉钉/飞书可用加签；企业微信无签名机制，不填。</div>
        </el-form-item>
        <el-form-item label="项目">
          <el-select
            v-model="form.project_id"
            clearable
            value-on-clear="null"
            :placeholder="projects.length ? '请选择项目' : '暂无项目'"
            :loading="projectsLoading"
            style="width: 100%"
          >
            <el-option label="全局（应用到全部项目）" :value="null" />
            <el-option
              v-for="p in projects"
              :key="p.id"
              :label="p.repo_full_name"
              :value="p.id"
            />
          </el-select>
          <div class="form-tip">不选则应用到全部项目（全局）。</div>
        </el-form-item>
        <el-form-item label="@阈值">
          <el-input-number v-model="form.at_threshold" :min="0" />
          <div class="form-tip">评分低于此阈值才触发 @；达标则不 @，避免打扰。</div>
        </el-form-item>
        <el-form-item label="指定成员">
          <el-select v-model="form.at_member_ids" multiple filterable style="width: 100%"
            :placeholder="members.length ? '选择该渠道要 @ 的人（评分低于阈值时触发）' : '请先在「@成员管理」添加'">
            <el-option v-for="m in members" :key="m.id" :value="m.id" :label="memberLabel(m)">
              <span>{{ m.name || m.git_username || '成员 #' + m.id }}</span>
              <el-tag size="small" type="info" class="member-tag">{{ m.dingtalk_mobile || '—' }} / {{ m.wecom_userid || '—' }} / {{ m.feishu_open_id || '—' }}</el-tag>
            </el-option>
          </el-select>
          <div class="form-tip">评分低于阈值时才 @ 这些成员 + @提交者；fork 用户名只作正文点名，不进 @ID。</div>
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

    <!-- 系统级 @成员管理 -->
    <el-dialog v-model="memberDialogVisible" title="系统级 @成员管理" width="860px">
      <div class="toolbar">
        <el-button type="primary" @click="openMemberForm()">新增成员</el-button>
      </div>
      <el-table :data="members" v-loading="membersLoading" stripe>
        <el-table-column prop="name" label="姓名" width="120" />
        <el-table-column prop="git_username" label="fork用户名" width="140" />
        <el-table-column prop="dingtalk_mobile" label="钉钉手机号" width="130" />
        <el-table-column prop="wecom_userid" label="企微userid" min-width="120" />
        <el-table-column prop="feishu_open_id" label="飞书open_id" min-width="130" />
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openMemberForm(row)">编辑</el-button>
            <el-button link type="danger" @click="onDeleteMember(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <el-dialog v-model="memberFormVisible" :title="memberIsEdit ? '编辑成员' : '新增成员'" width="520px"
      :modal="false" append-to-body>
      <el-form :model="memberForm" label-width="120px">
        <el-form-item label="姓名"><el-input v-model="memberForm.name" placeholder="展示名，可空" /></el-form-item>
        <el-form-item label="fork用户名"><el-input v-model="memberForm.git_username" placeholder="提交者 @ 的命中键，如 zhansan379" /></el-form-item>
        <el-form-item label="钉钉手机号"><el-input v-model="memberForm.dingtalk_mobile" placeholder="钉钉 @ 用" /></el-form-item>
        <el-form-item label="企微userid"><el-input v-model="memberForm.wecom_userid" placeholder="企微点名用" /></el-form-item>
        <el-form-item label="飞书open_id"><el-input v-model="memberForm.feishu_open_id" placeholder="飞书 @ 用" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="memberFormVisible = false">取消</el-button>
        <el-button type="primary" :loading="memberSaving" @click="onSaveMember">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listNotifiers, createNotifier, updateNotifier, deleteNotifier, listProjects,
  listMembers, createMember, updateMember, deleteMember,
  type Notifier, type Project, type NotifierMember,
} from '../api'

const items = ref<Notifier[]>([])
const projects = ref<Project[]>([])
const projectsLoading = ref(false)
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const editingId = ref<number | null>(null)

// —— 系统级 @成员管理 ——
const members = ref<NotifierMember[]>([])
const membersLoading = ref(false)
const memberDialogVisible = ref(false)
const memberFormVisible = ref(false)
const memberSaving = ref(false)
const memberIsEdit = ref(false)
const memberEditingId = ref<number | null>(null)
const memberForm = reactive({ name: '', git_username: '', dingtalk_mobile: '', wecom_userid: '', feishu_open_id: '' })

const REDACTED = '******'

const emptyForm = () => ({
  channel: 'dingtalk' as 'dingtalk' | 'feishu' | 'wecom',
  webhook: '',
  secret: '',
  project_id: null as number | null,
  at_threshold: 60,
  enabled: true,
  at_member_ids: [] as number[],
})
const form = reactive(emptyForm())

async function load() {
  loading.value = true
  try {
    items.value = await listNotifiers()
  } finally {
    loading.value = false
  }
}

async function loadProjects() {
  projectsLoading.value = true
  try {
    projects.value = await listProjects()
  } finally {
    projectsLoading.value = false
  }
}

async function loadMembers() {
  membersLoading.value = true
  try {
    members.value = await listMembers()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载成员失败')
  } finally {
    membersLoading.value = false
  }
}

function openMemberManager() {
  memberDialogVisible.value = true
  loadMembers()
}
function openMemberForm(row?: NotifierMember) {
  memberIsEdit.value = !!row
  memberEditingId.value = row?.id ?? null
  Object.assign(memberForm, {
    name: row?.name ?? '',
    git_username: row?.git_username ?? '',
    dingtalk_mobile: row?.dingtalk_mobile ?? '',
    wecom_userid: row?.wecom_userid ?? '',
    feishu_open_id: row?.feishu_open_id ?? '',
  })
  memberFormVisible.value = true
}
async function onSaveMember() {
  memberSaving.value = true
  try {
    if (memberIsEdit.value && memberEditingId.value != null) {
      await updateMember(memberEditingId.value, { ...memberForm })
    } else {
      await createMember({ ...memberForm })
    }
    ElMessage.success('已保存成员')
    memberFormVisible.value = false
    loadMembers()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存成员失败')
  } finally {
    memberSaving.value = false
  }
}
async function onDeleteMember(row: NotifierMember) {
  await ElMessageBox.confirm(`确认删除成员「${row.name || row.git_username || row.id}」？`, '提示', { type: 'warning' })
  await deleteMember(row.id)
  ElMessage.success('已删除')
  loadMembers()
}

function memberLabel(m: NotifierMember): string {
  if (m.name) return `${m.name}${m.git_username ? '（@' + m.git_username + '）' : ''}`
  return m.git_username || `成员 #${m.id}`
}

function openCreate() {
  isEdit.value = false
  editingId.value = null
  Object.assign(form, emptyForm())
  loadProjects()
  dialogVisible.value = true
}
function openEdit(row: Notifier) {
  isEdit.value = true
  editingId.value = row.id
  loadProjects()
  Object.assign(form, {
    channel: row.channel,
    webhook: row.webhook || REDACTED,
    // 企业微信无签名：secret 直接置空，避免回显占位误导
    secret: row.channel === 'wecom' ? '' : (row.secret || REDACTED),
    project_id: row.project_id ?? null,
    at_threshold: row.at_threshold ?? 60,
    enabled: row.enabled,
    at_member_ids: row.at_member_ids || [],
  })
  dialogVisible.value = true
}
function onChannelChange() {
  // 切到企业微信时清掉残留 secret（该渠道无签名）
  if (form.channel === 'wecom') {
    form.secret = ''
  }
}

async function onSave() {
  // 新建时 webhook 必填
  if (!isEdit.value && !form.webhook) {
    ElMessage.warning('请输入 Webhook 地址')
    return
  }
  saving.value = true
  try {
    if (isEdit.value && editingId.value != null) {
      // 占位 ****** 原样提交，后端按「不修改」处理
      await updateNotifier(editingId.value, { ...form })
    } else {
      await createNotifier({ ...form })
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

async function onDelete(row: Notifier) {
  await ElMessageBox.confirm(`确认删除「${row.channel}」通知渠道？`, '提示', { type: 'warning' })
  await deleteNotifier(row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(() => {
  load()
  loadProjects()
  loadMembers()
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
.member-tag {
  margin-left: 8px;
  font-variant-numeric: tabular-nums;
}
</style>