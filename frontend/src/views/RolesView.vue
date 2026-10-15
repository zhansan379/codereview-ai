<template>
  <div>
    <el-card>
      <div class="toolbar" data-tour="roles-toolbar">
        <el-button v-perm="'roles:edit'" type="primary" @click="openCreate">{{ $t('roles.createTitle') }}</el-button>
      </div>
      <el-table :data="items" v-loading="loading" stripe data-tour="roles-table">
        <el-table-column prop="name" :label="$t('common.name')" min-width="110" />
        <el-table-column prop="description" :label="$t('roles.description')" min-width="150" show-overflow-tooltip />
        <el-table-column :label="$t('roles.type')" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_system ? 'info' : 'warning'" size="small">
              {{ row.is_system ? $t('roles.builtin') : $t('roles.custom') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('roles.allProjects')" :width="colWidth(90)">
          <template #default="{ row }">
            <el-tag v-if="row.all_projects" type="success" size="small">{{ $t('common.yes') }}</el-tag>
            <span v-else class="muted">{{ $t('common.no') }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('roles.permCount')" :width="colWidth(85)">
          <template #default="{ row }">
            {{ row.permissions.length }}
          </template>
        </el-table-column>
        <el-table-column prop="member_count" :label="$t('roles.memberCount')" :width="colWidth(80)" />
        <el-table-column :label="$t('common.actions')" :width="colWidth(200)" fixed="right">
          <template #default="{ row }">
            <el-button v-perm="'roles:edit'" link type="primary" @click="openPerms(row)">{{ $t('roles.permissions') }}</el-button>
            <el-button v-if="!row.is_system" v-perm="'roles:edit'" link type="primary" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button v-if="!row.is_system" v-perm="'roles:edit'" link type="danger" @click="onDelete(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新增 / 编辑 角色 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? $t('roles.editTitle') : $t('roles.createTitle')" width="640px">
      <el-form :model="form" label-width="auto">
        <el-form-item :label="$t('common.name')" required>
          <el-input v-model="form.name" :disabled="isEdit" />
        </el-form-item>
        <el-form-item :label="$t('roles.description')">
          <el-input v-model="form.description" />
        </el-form-item>
        <el-form-item v-if="!isEdit" :label="$t('roles.allProjects')">
          <el-switch v-model="form.all_projects" />
          <span class="form-tip">{{ $t('roles.allProjectsTip') }}</span>
        </el-form-item>
        <el-form-item v-if="!isEdit" :label="$t('roles.permissions')">
          <PermissionTreeSelect v-model="form.permission_codes" :nodes="perms" :loading="permsLoading" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">{{ isEdit ? $t('common.save') : $t('roles.createBtn') }}</el-button>
      </template>
    </el-dialog>

    <!-- 权限分配（树形，任意角色含内置） -->
    <el-dialog
      v-model="permVisible"
      :title="$t('roles.assignPermsTitle', { name: current?.name || '' })"
      width="640px"
      :close-on-click-modal="false"
      @closed="onPermsClosed"
    >
      <!-- 角色信息：只读回显 -->
      <div class="perm-role">
        <el-tag type="info" size="small">{{ $t('common.name') }}：{{ current?.name }}</el-tag>
        <el-tag type="info" size="small">
          {{ $t('roles.code') }}：{{ current?.builtin_code || $t('roles.custom') }}
        </el-tag>
      </div>

      <PermissionTreeSelect v-model="permCodes" :nodes="perms" :loading="permsLoading" />

      <template #footer>
        <el-button :disabled="saving" @click="permVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" :disabled="!perms.length" @click="onSavePerms">
          {{ $t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { colWidth } from '../composables/useLocale'
import PermissionTreeSelect from '../components/PermissionTreeSelect.vue'
import {
  listRoles,
  listPermissions,
  createRole,
  updateRole,
  deleteRole,
  setRolePermissions,
  type RoleItem,
  type PermissionNode,
} from '../api'

const { t } = useI18n()

const items = ref<RoleItem[]>([])
const perms = ref<PermissionNode[]>([])
const loading = ref(false)
const permsLoading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const current = ref<RoleItem | null>(null)
const permVisible = ref(false)
// 权限分配弹窗当前勾选的权限码（由 PermissionTreeSelect 双向绑定）
const permCodes = ref<string[]>([])

const emptyForm = () => ({ name: '', description: '', all_projects: false, permission_codes: [] as string[] })
const form = reactive(emptyForm())

async function load() {
  loading.value = true
  permsLoading.value = true
  try {
    items.value = await listRoles()
    perms.value = await listPermissions()
  } finally {
    loading.value = false
    permsLoading.value = false
  }
}

function openCreate() {
  isEdit.value = false
  current.value = null
  Object.assign(form, emptyForm())
  dialogVisible.value = true
}
function openEdit(row: RoleItem) {
  isEdit.value = true
  current.value = row
  Object.assign(form, {
    name: row.name, description: row.description, all_projects: row.all_projects, permission_codes: [],
  })
  dialogVisible.value = true
}

async function onSave() {
  saving.value = true
  try {
    if (isEdit.value && current.value) {
      await updateRole(current.value.id, { description: form.description })
    } else {
      await createRole({
        name: form.name, description: form.description,
        all_projects: form.all_projects, permission_codes: form.permission_codes,
      })
    }
    ElMessage.success(isEdit.value ? t('common.saved') : t('common.created'))
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    saving.value = false
  }
}

function openPerms(row: RoleItem) {
  current.value = row
  permCodes.value = [...row.permissions]
  permVisible.value = true
}

async function onSavePerms() {
  if (saving.value) return
  saving.value = true
  try {
    await setRolePermissions(current.value!.id, [...permCodes.value])
    ElMessage.success(t('roles.permsUpdated'))
    permVisible.value = false
    load()
  } catch (e: any) {
    // 保存失败：保留弹窗与勾选，仅提示错误
    ElMessage.error(e?.response?.data?.detail || t('common.saveFailed'))
  } finally {
    saving.value = false
  }
}

// 弹窗关闭后清空勾选，避免下次打开残留
function onPermsClosed() {
  permCodes.value = []
}

async function onDelete(row: RoleItem) {
  try {
    await ElMessageBox.confirm(t('roles.deleteConfirm', { name: row.name }), t('common.tip'), { type: 'warning' })
    await deleteRole(row.id)
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
/* 权限分配弹窗：角色回显标签 */
.perm-role {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.form-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-left: 10px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>