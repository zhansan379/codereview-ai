<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">{{ $t('notifiers.createBtn') }}</el-button>
        <el-button @click="openMemberManager">{{ $t('notifiers.memberManagerBtn') }}</el-button>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" :label="$t('common.id')" width="70" />
        <el-table-column :label="$t('notifiers.columns.channel')" width="120">
          <template #default="{ row }">
            <el-tag>{{ row.channel }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="project_id" :label="$t('notifiers.columns.project')" :width="colWidth(110)">
          <template #default="{ row }">
            {{ row.project_id === null ? $t('notifiers.columns.global') : row.project_id }}
          </template>
        </el-table-column>
        <el-table-column prop="at_threshold" :label="$t('notifiers.columns.atThreshold')" :width="colWidth(90)" />
        <el-table-column :label="$t('notifiers.columns.atAll')" :width="colWidth(100)">
          <template #default="{ row }">
            <el-tag :type="row.at_all ? 'warning' : 'info'">
              {{ row.at_all ? $t('common.yes') : $t('common.no') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('notifiers.columns.enabled')" :width="colWidth(80)">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? $t('common.yes') : $t('common.no') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="webhook" label="Webhook" min-width="160" show-overflow-tooltip />
        <el-table-column :label="$t('common.actions')" width="160" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button link type="danger" @click="onDelete(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? $t('notifiers.dialog.editTitle') : $t('notifiers.dialog.createTitle')" width="560px">
      <el-form :model="form" label-width="auto">
        <el-form-item :label="$t('notifiers.form.channel')" required>
          <el-select
            v-model="form.channel"
            :disabled="isEdit"
            style="width: 100%"
            @change="onChannelChange"
          >
            <el-option :label="$t('notifiers.form.channelDingtalk')" value="dingtalk" />
            <el-option :label="$t('notifiers.form.channelFeishu')" value="feishu" />
            <el-option :label="$t('notifiers.form.channelWecom')" value="wecom" />
          </el-select>
        </el-form-item>
        <el-form-item label="Webhook" required>
          <el-input
            v-model="form.webhook"
            type="password"
            show-password
            :placeholder="isEdit ? $t('notifiers.form.webhookKeepPlaceholder') : $t('notifiers.form.webhookPlaceholder')"
          />
          <div class="form-tip" v-if="isEdit">{{ $t('notifiers.form.redactedTip') }}</div>
        </el-form-item>
        <el-form-item v-if="form.channel !== 'wecom'" label="Secret" required>
          <el-input
            v-model="form.secret"
            type="password"
            show-password
            :placeholder="isEdit ? $t('notifiers.form.webhookKeepPlaceholder') : $t('notifiers.form.secretPlaceholder')"
          />
          <div class="form-tip" v-if="isEdit">{{ $t('notifiers.form.redactedTip') }}</div>
          <div class="form-tip" v-else>{{ $t('notifiers.form.secretTip') }}</div>
        </el-form-item>
        <el-form-item :label="$t('notifiers.form.project')">
          <el-select
            v-model="form.project_id"
            clearable
            :value-on-clear="null"
            :placeholder="projects.length ? $t('notifiers.form.projectPlaceholder') : $t('notifiers.form.projectEmptyPlaceholder')"
            :loading="projectsLoading"
            style="width: 100%"
          >
            <el-option :label="$t('notifiers.form.projectGlobalOption')" :value="null" />
            <el-option
              v-for="p in projects"
              :key="p.id"
              :label="p.repo_full_name"
              :value="p.id"
            />
          </el-select>
          <div class="form-tip">{{ $t('notifiers.form.projectTip') }}</div>
        </el-form-item>
        <el-form-item :label="$t('notifiers.form.atThreshold')">
          <el-input-number v-model="form.at_threshold" :min="0" />
          <div class="form-tip">{{ $t('notifiers.form.atThresholdTip') }}</div>
        </el-form-item>
        <el-form-item :label="$t('notifiers.form.atAll')">
          <el-switch v-model="form.at_all" />
          <div class="form-tip">{{ $t('notifiers.form.atAllTip') }}</div>
        </el-form-item>
        <el-form-item :label="$t('notifiers.form.atMembers')">
          <el-select v-model="form.at_member_ids" multiple filterable style="width: 100%"
            :placeholder="members.length ? $t('notifiers.form.atMembersPlaceholder') : $t('notifiers.form.atMembersEmptyPlaceholder')">
            <el-option v-for="m in members" :key="m.id" :value="m.id" :label="memberLabel(m)">
              <span>{{ m.name || m.git_username || $t('notifiers.members.fallbackLabel', { id: m.id }) }}</span>
              <el-tag size="small" type="info" class="member-tag">{{ m.dingtalk_mobile || '—' }} / {{ m.wecom_userid || '—' }} / {{ m.feishu_open_id || '—' }}</el-tag>
            </el-option>
          </el-select>
          <div class="form-tip">{{ $t('notifiers.form.atMembersTip') }}</div>
        </el-form-item>
        <el-form-item :label="$t('notifiers.form.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 系统级 @成员管理（表格内联编辑：点行内「编辑」该行变输入框；新增即追加一草稿行） -->
    <el-dialog v-model="memberDialogVisible" :title="$t('notifiers.members.title')" width="880px">
      <div class="toolbar">
        <el-button type="primary" @click="addMember">{{ $t('notifiers.members.addBtn') }}</el-button>
      </div>
      <el-table :data="displayMembers" v-loading="membersLoading" stripe
        :empty-text="members.length || newRowDraft ? ' ' : $t('notifiers.members.empty')">
        <el-table-column :label="$t('notifiers.members.name')" width="120">
          <template #default="{ row }">
            <el-input v-if="memberEditingId === row.id" v-model="memberForm.name" :placeholder="$t('notifiers.members.namePlaceholder')" size="small" clearable style="width: 100%" />
            <template v-else>{{ row.name || '—' }}</template>
          </template>
        </el-table-column>
        <el-table-column :label="$t('notifiers.members.gitUsername')" width="150">
          <template #default="{ row }">
            <el-input v-if="memberEditingId === row.id" v-model="memberForm.git_username" :placeholder="$t('notifiers.members.gitUsernamePlaceholder')" size="small" clearable style="width: 100%" />
            <template v-else>{{ row.git_username || '—' }}</template>
          </template>
        </el-table-column>
        <el-table-column :label="$t('notifiers.members.dingtalkMobile')" :width="colWidth(140)">
          <template #default="{ row }">
            <el-input v-if="memberEditingId === row.id" v-model="memberForm.dingtalk_mobile" :placeholder="$t('notifiers.members.dingtalkMobilePlaceholder')" size="small" clearable style="width: 100%" />
            <template v-else>{{ row.dingtalk_mobile || '—' }}</template>
          </template>
        </el-table-column>
        <el-table-column :label="$t('notifiers.members.wecomUserid')" min-width="120">
          <template #default="{ row }">
            <el-input v-if="memberEditingId === row.id" v-model="memberForm.wecom_userid" :placeholder="$t('notifiers.members.wecomUseridPlaceholder')" size="small" clearable style="width: 100%" />
            <template v-else>{{ row.wecom_userid || '—' }}</template>
          </template>
        </el-table-column>
        <el-table-column :label="$t('notifiers.members.feishuOpenId')" min-width="130">
          <template #default="{ row }">
            <el-input v-if="memberEditingId === row.id" v-model="memberForm.feishu_open_id" :placeholder="$t('notifiers.members.feishuOpenIdPlaceholder')" size="small" clearable style="width: 100%" />
            <template v-else>{{ row.feishu_open_id || '—' }}</template>
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.actions')" width="150" fixed="right">
          <template #default="{ row }">
            <template v-if="memberEditingId === row.id">
              <el-button link type="success" :loading="memberSaving" @click="onSaveInline(row)">{{ $t('common.save') }}</el-button>
              <el-button link @click="cancelInline(row)">{{ $t('common.cancel') }}</el-button>
            </template>
            <template v-else>
              <el-button link type="primary" @click="startEdit(row)">{{ $t('common.edit') }}</el-button>
              <el-button link type="danger" @click="onDeleteMember(row)">{{ $t('common.delete') }}</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { colWidth } from '../composables/useLocale'
import {
  listNotifiers, createNotifier, updateNotifier, deleteNotifier, listProjects,
  listMembers, createMember, updateMember, deleteMember,
  type Notifier, type Project, type NotifierMember,
} from '../api'

const { t } = useI18n()

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
/** 正在内联编辑的成员 id；表格行内显示输入框 */
const memberEditingId = ref<number | null>(null)
/** 新增中的草稿行（id=0 哨兵）；非空时作为表格最后一行渲染输入框 */
const newRowDraft = ref<NotifierMember | null>(null)
const memberSaving = ref(false)
const memberForm = reactive({ name: '', git_username: '', dingtalk_mobile: '', wecom_userid: '', feishu_open_id: '' })

/** 表格绑定数据：有草稿行就临时拼进末尾，避免 data 为空触发空数据占位块 */
const displayMembers = computed<NotifierMember[]>(() =>
  newRowDraft.value ? [...members.value, newRowDraft.value] : members.value,
)

const REDACTED = '******'

const emptyForm = () => ({
  channel: 'dingtalk' as 'dingtalk' | 'feishu' | 'wecom',
  webhook: '',
  secret: '',
  project_id: null as number | null,
  at_threshold: 60,
  at_all: false,
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
  } catch (e: any) {
    projects.value = []
    ElMessage.error(e?.message || t('notifiers.msg.projectsLoadFailed'))
  } finally {
    projectsLoading.value = false
  }
}

async function loadMembers() {
  membersLoading.value = true
  try {
    members.value = await listMembers()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('notifiers.members.loadFailed'))
  } finally {
    membersLoading.value = false
  }
}

function openMemberManager() {
  memberDialogVisible.value = true
  loadMembers()
}
function startEdit(row: NotifierMember) {
  newRowDraft.value = null
  memberEditingId.value = row.id
  Object.assign(memberForm, {
    name: row.name ?? '',
    git_username: row.git_username ?? '',
    dingtalk_mobile: row.dingtalk_mobile ?? '',
    wecom_userid: row.wecom_userid ?? '',
    feishu_open_id: row.feishu_open_id ?? '',
  })
}
function addMember() {
  newRowDraft.value = { id: 0, name: '', git_username: '', dingtalk_mobile: '', wecom_userid: '', feishu_open_id: '' }
  Object.assign(memberForm, { name: '', git_username: '', dingtalk_mobile: '', wecom_userid: '', feishu_open_id: '' })
  memberEditingId.value = 0
}
function cancelInline(row: NotifierMember) {
  if (row.id === 0) newRowDraft.value = null
  else memberEditingId.value = null
}
async function onSaveInline(row: NotifierMember) {
  memberSaving.value = true
  try {
    if (row.id === 0) {
      await createMember({ ...memberForm })
      ElMessage.success(t('notifiers.members.added'))
      newRowDraft.value = null
    } else {
      await updateMember(row.id, { ...memberForm })
      ElMessage.success(t('notifiers.members.saved'))
      memberEditingId.value = null
    }
    loadMembers()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('notifiers.members.saveFailed'))
  } finally {
    memberSaving.value = false
  }
}
async function onDeleteMember(row: NotifierMember) {
  await ElMessageBox.confirm(
    t('notifiers.members.deleteConfirm', { name: row.name || row.git_username || row.id }),
    t('common.tip'),
    { type: 'warning' },
  )
  await deleteMember(row.id)
  ElMessage.success(t('common.deleted'))
  loadMembers()
}

function memberLabel(m: NotifierMember): string {
  if (m.name) return m.git_username ? t('notifiers.members.labelWithGit', { name: m.name, git: m.git_username }) : m.name
  return m.git_username || t('notifiers.members.fallbackLabel', { id: m.id })
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
    at_all: !!row.at_all,
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
    ElMessage.warning(t('notifiers.msg.webhookRequired'))
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
    ElMessage.success(t('notifiers.msg.saveSuccess'))
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function onDelete(row: Notifier) {
  await ElMessageBox.confirm(t('notifiers.msg.deleteConfirm', { channel: row.channel }), t('common.tip'), { type: 'warning' })
  await deleteNotifier(row.id)
  ElMessage.success(t('common.deleted'))
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
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.member-tag {
  margin-left: 8px;
  font-variant-numeric: tabular-nums;
}
</style>