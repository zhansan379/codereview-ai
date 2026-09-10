<template>
  <div>
    <el-card>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">新增角色</el-button>
      </div>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column prop="description" label="描述" min-width="150" show-overflow-tooltip />
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_system ? 'info' : 'warning'" size="small">
              {{ row.is_system ? '内置' : '自定义' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="全项目" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.all_projects" type="success" size="small">是</el-tag>
            <span v-else class="muted">否</span>
          </template>
        </el-table-column>
        <el-table-column label="权限数" width="80">
          <template #default="{ row }">
            {{ row.permissions.length }}
          </template>
        </el-table-column>
        <el-table-column prop="member_count" label="成员数" width="80" />
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openPerms(row)">权限</el-button>
            <el-button v-if="!row.is_system" link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button v-if="!row.is_system" link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新增 / 编辑 角色 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑角色' : '新增角色'" width="640px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" :disabled="isEdit" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" />
        </el-form-item>
        <el-form-item v-if="!isEdit" label="全项目">
          <el-switch v-model="form.all_projects" />
          <span class="form-tip">勾选后该角色的项目级权限对所有项目生效（无需逐项目加成员）。</span>
        </el-form-item>
        <el-form-item v-if="!isEdit" label="权限">
          <div class="perm-panel">
            <div v-for="group in permGroups" :key="group.scope" class="perm-group">
              <div class="perm-group-title">{{ group.label }}</div>
              <el-checkbox-group v-model="form.permission_codes">
                <el-checkbox v-for="p in group.items" :key="p.code" :value="p.code">
                  {{ p.name }} <span class="muted">· {{ p.code }}</span>
                </el-checkbox>
              </el-checkbox-group>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">{{ isEdit ? '保存' : '创建' }}</el-button>
      </template>
    </el-dialog>

    <!-- 权限分配（任意角色，含内置） -->
    <el-dialog v-model="permVisible" :title="`分配权限：${current?.name || ''}`" width="640px">
      <div class="perm-panel">
        <div v-for="group in permGroups" :key="group.scope" class="perm-group">
          <div class="perm-group-title">{{ group.label }}</div>
          <el-checkbox-group v-model="permForm.codes">
            <el-checkbox v-for="p in group.items" :key="p.code" :value="p.code">
              {{ p.name }} <span class="muted">· {{ p.code }}</span>
            </el-checkbox>
          </el-checkbox-group>
        </div>
      </div>
      <template #footer>
        <el-button @click="permVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSavePerms">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listRoles,
  listPermissions,
  createRole,
  updateRole,
  deleteRole,
  setRolePermissions,
  type RoleItem,
  type PermissionItem,
} from '../api'

const items = ref<RoleItem[]>([])
const perms = ref<PermissionItem[]>([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const current = ref<RoleItem | null>(null)
const permVisible = ref(false)

const permGroups = computed(() => [
  {
    scope: 'global',
    label: '全局权限（系统级，不区分项目）',
    items: perms.value.filter((p) => p.scope === 'global'),
  },
  {
    scope: 'project',
    label: '项目权限（经成员关系 / 全项目角色生效）',
    items: perms.value.filter((p) => p.scope === 'project'),
  },
])

const emptyForm = () => ({ name: '', description: '', all_projects: false, permission_codes: [] as string[] })
const form = reactive(emptyForm())

const permForm = reactive({ codes: [] as string[] })

async function load() {
  loading.value = true
  try {
    items.value = await listRoles()
    perms.value = await listPermissions()
  } finally {
    loading.value = false
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
    ElMessage.success(isEdit.value ? '已保存' : '已创建')
    dialogVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

function openPerms(row: RoleItem) {
  current.value = row
  permForm.codes = [...row.permissions]
  permVisible.value = true
}
async function onSavePerms() {
  saving.value = true
  try {
    await setRolePermissions(current.value!.id, permForm.codes)
    ElMessage.success('权限已更新')
    permVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function onDelete(row: RoleItem) {
  try {
    await ElMessageBox.confirm(`确认删除角色「${row.name}」？`, '提示', { type: 'warning' })
    await deleteRole(row.id)
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
.form-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-left: 10px;
}
.perm-panel {
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
  max-height: 480px;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 12px;
}
.perm-group {
  margin-bottom: 12px;
}
.perm-group:last-child {
  margin-bottom: 0;
}
.perm-group-title {
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--el-text-color-primary);
}
/* 权限多选默认 white-space:nowrap 且横向排布，标签长时会撑破对话框。
   改用 flex 换行 + 标签可断词，保证面板宽度恒不超出容器。 */
.perm-panel :deep(.el-checkbox-group) {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 20px;
}
.perm-panel :deep(.el-checkbox) {
  margin-right: 0;
  min-width: 0;
  white-space: normal;
}
.perm-panel :deep(.el-checkbox__label) {
  white-space: normal;
  word-break: break-word;
  line-height: 20px;
  min-width: 0;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>