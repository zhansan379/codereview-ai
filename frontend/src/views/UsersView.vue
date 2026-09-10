<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">新增用户</el-button>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column prop="display_name" label="显示名" min-width="110" />
        <el-table-column prop="role_name" label="角色" width="120" show-overflow-tooltip />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="160">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="260" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="primary" @click="openResetPw(row)">重置密码</el-button>
            <el-button link :type="row.enabled ? 'warning' : 'success'" @click="toggleEnabled(row)">
              {{ row.enabled ? '禁用' : '启用' }}
            </el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑用户' : '新增用户'" width="520px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="用户名" required>
          <el-input v-model="form.username" :disabled="isEdit" />
        </el-form-item>
        <el-form-item v-if="!isEdit" label="初始密码" required>
          <el-input v-model="form.password" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" />
        </el-form-item>
        <el-form-item label="角色" required>
          <el-select v-model="form.role_id" style="width: 100%">
            <el-option v-for="r in roles" :key="r.id" :label="`${r.name}${r.is_system ? '（内置）' : ''}`" :value="r.id" />
          </el-select>
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

    <!-- 重置密码 -->
    <el-dialog v-model="pwVisible" :title="`重置密码：${current?.username || ''}`" width="480px">
      <el-form label-width="90px">
        <el-form-item label="新密码" required>
          <el-input v-model="newPassword" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onResetPw">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
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
    ElMessage.success('保存成功')
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(row: UserRow) {
  try {
    await updateUser(row.id, { enabled: !row.enabled })
    ElMessage.success(row.enabled ? '已禁用' : '已启用')
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
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
    ElMessage.success('密码已重置')
    pwVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重置失败')
  } finally {
    saving.value = false
  }
}

async function onDelete(row: UserRow) {
  try {
    await ElMessageBox.confirm(`确认删除用户「${row.username}」？`, '提示', { type: 'warning' })
    await deleteUser(row.id)
    ElMessage.success('已删除')
    load()
  } catch (e: any) {
    if (e !== 'cancel') ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  margin-bottom: 12px;
}
</style>