<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">{{ $t('users.createTitle') }}</el-button>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" :label="$t('common.id')" width="70" />
        <el-table-column prop="username" :label="$t('users.username')" min-width="120" />
        <el-table-column prop="display_name" :label="$t('users.displayName')" min-width="110" />
        <el-table-column prop="role_name" :label="$t('users.role')" width="120" show-overflow-tooltip />
        <el-table-column :label="$t('common.status')" width="90">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? $t('users.enabledTag') : $t('users.disabledTag') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.createdAt')" min-width="160">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
        <el-table-column :label="$t('common.actions')" :width="colWidth(260)" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button link type="primary" @click="openResetPw(row)">{{ $t('users.resetPassword') }}</el-button>
            <el-button link :type="row.enabled ? 'warning' : 'success'" @click="toggleEnabled(row)">
              {{ row.enabled ? $t('users.disable') : $t('users.enable') }}
            </el-button>
            <el-button link type="danger" @click="onDelete(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? $t('users.editTitle') : $t('users.createTitle')" width="520px">
      <el-form :model="form" label-width="auto">
        <el-form-item :label="$t('users.username')" required>
          <el-input v-model="form.username" :disabled="isEdit" />
        </el-form-item>
        <el-form-item v-if="!isEdit" :label="$t('users.initialPassword')" required>
          <el-input v-model="form.password" type="password" show-password :placeholder="$t('users.passwordHint')" />
        </el-form-item>
        <el-form-item :label="$t('users.displayName')">
          <el-input v-model="form.display_name" />
        </el-form-item>
        <el-form-item :label="$t('users.role')" required>
          <el-select v-model="form.role_id" style="width: 100%">
            <el-option
              v-for="r in roles"
              :key="r.id"
              :label="r.is_system ? $t('users.roleOptionSystem', { name: r.name }) : r.name"
              :value="r.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('users.enabledField')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog
      v-model="pwVisible"
      :title="$t('users.resetPasswordTitle', { name: current?.username || '' })"
      width="480px"
    >
      <el-form label-width="auto">
        <el-form-item :label="$t('users.newPassword')" required>
          <el-input v-model="newPassword" type="password" show-password :placeholder="$t('users.passwordHint')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="onResetPw">{{ $t('common.confirm') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import {
  listUsers,
  createUser,
  updateUser,
  deleteUser,
  resetPassword,
  listRoles,
  type UserRow,
  type RoleItem,
} from '../api'
import { formatTime } from '../utils/format'
import { colWidth } from '../composables/useLocale'

const { t } = useI18n()

const fmt = formatTime

const items = ref<UserRow[]>([])
const roles = ref<RoleItem[]>([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const current = ref<UserRow | null>(null)
const pwVisible = ref(false)
const newPassword = ref('')

const emptyForm = () => ({ username: '', password: '', display_name: '', role_id: 0, enabled: true })
const form = reactive(emptyForm())

async function load() {
  loading.value = true
  try {
    items.value = await listUsers()
    roles.value = await listRoles()
  } finally {
    loading.value = false
  }
}

function openCreate() {
  isEdit.value = false
  current.value = null
  Object.assign(form, emptyForm())
  if (roles.value.length) form.role_id = roles.value[0].id
  dialogVisible.value = true
}
function openEdit(row: UserRow) {
  isEdit.value = true
  current.value = row
  Object.assign(form, {
    username: row.username, password: '', display_name: row.display_name,
    role_id: row.role_id, enabled: row.enabled,
  })
  dialogVisible.value = true
}

async function onSave() {
  saving.value = true
  try {
    if (isEdit.value && current.value) {
      await updateUser(current.value.id, {
        display_name: form.display_name,
        role_id: form.role_id,
        enabled: form.enabled,
      })
    } else {
      await createUser({
        username: form.username, password: form.password,
        display_name: form.display_name, role_id: form.role_id, enabled: form.enabled,
      })
    }
    ElMessage.success(t('users.saveSuccess'))
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(row: UserRow) {
  try {
    await updateUser(row.id, { enabled: !row.enabled })
    ElMessage.success(row.enabled ? t('users.disabledDone') : t('users.enabledDone'))
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.operationFailed'))
  }
}

function openResetPw(row: UserRow) {
  current.value = row
  newPassword.value = ''
  pwVisible.value = true
}
async function onResetPw() {
  saving.value = true
  try {
    await resetPassword(current.value!.id, newPassword.value)
    ElMessage.success(t('users.passwordReset'))
    pwVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('users.resetFailed'))
  } finally {
    saving.value = false
  }
}

async function onDelete(row: UserRow) {
  try {
    await ElMessageBox.confirm(t('users.deleteConfirm', { name: row.username }), t('common.tip'), { type: 'warning' })
    await deleteUser(row.id)
    ElMessage.success(t('common.deleted'))
    load()
  } catch (e: any) {
    if (e !== 'cancel') ElMessage.error(e?.response?.data?.detail || t('common.deleteFailed'))
  }
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  margin-bottom: 12px;
}
</style>