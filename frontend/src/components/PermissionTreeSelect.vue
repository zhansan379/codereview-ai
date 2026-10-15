<template>
  <div class="pts">
    <!-- 操作开关：全选/全不选、全部展开/折叠 -->
    <div class="pts-toolbar">
      <span class="pts-switch">
        <el-switch v-model="checkAll" :disabled="!allCodes.length" @change="onToggleCheckAll" />
        <span class="pts-switch-label">{{ $t('roles.selectAll') }}</span>
      </span>
      <span class="pts-switch">
        <el-switch v-model="expandAll" :disabled="!allCodes.length" @change="onToggleExpandAll" />
        <span class="pts-switch-label">{{ $t('roles.expandAll') }}</span>
      </span>
    </div>

    <!-- 树形权限列表（多级嵌套，叶子为可勾选权限码） -->
    <div class="pts-panel" v-loading="loading">
      <el-empty
        v-if="!loading && !nodes.length"
        :description="$t('roles.noPerms')"
        :image-size="60"
      />
      <el-tree
        v-else
        ref="treeRef"
        :data="treeData"
        node-key="key"
        show-checkbox
        :props="{ label: 'label', children: 'children' }"
        @check="onCheck"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
// 权限树选择器：v-model 绑定已选权限码；父节点仅为分组、不计入权限码。
import { computed, nextTick, ref, watch } from 'vue'
import { ElTree } from 'element-plus'
import type { PermissionNode } from '../api'

const props = defineProps<{
  nodes: PermissionNode[]
  modelValue: string[]
  loading?: boolean
}>()
const emit = defineEmits<{ (e: 'update:modelValue', value: string[]): void }>()

const treeRef = ref<InstanceType<typeof ElTree>>()
const checkAll = ref(false)
const expandAll = ref(false)

interface TreeNode {
  key: string
  label: string
  children?: TreeNode[]
}

const treeData = computed<TreeNode[]>(() => build(props.nodes))

function build(nodes: PermissionNode[]): TreeNode[] {
  return nodes.map((n) => {
    const children = n.children?.length ? build(n.children) : undefined
    return {
      key: n.code ?? `__group__${n.key}`,
      label: n.code ? `${n.name} · ${n.code}` : n.name,
      children,
    }
  })
}

// 所有叶子权限码（用于全选与全选状态判断）
const allCodes = computed<string[]>(() => collectCodes(props.nodes))
const codeSet = computed(() => new Set(allCodes.value))

function collectCodes(nodes: PermissionNode[]): string[] {
  const out: string[] = []
  for (const n of nodes) {
    if (n.code) out.push(n.code)
    if (n.children?.length) out.push(...collectCodes(n.children))
  }
  return out
}

// 成对授予：`:edit` 的成对 `:view`（该 view 存在时才配对）
function pairedView(code: string): string | null {
  if (!code.endsWith(':edit')) return null
  const v = code.slice(0, -':edit'.length) + ':view'
  return codeSet.value.has(v) ? v : null
}
function pairedEdit(code: string): string | null {
  if (!code.endsWith(':view')) return null
  const e = code.slice(0, -':view'.length) + ':edit'
  return codeSet.value.has(e) ? e : null
}

const lastChecked = ref<Set<string>>(new Set())

// 应用勾选集合（同步到树、更新全选开关、回传）
function applyChecked(checked: Set<string>) {
  const arr = [...checked]
  lastChecked.value = new Set(arr)
  treeRef.value?.setCheckedKeys(arr)
  checkAll.value = allCodes.value.length > 0 && arr.length === allCodes.value.length
  emit('update:modelValue', arr.sort())
}

// 外部值 → 勾选状态回显（含父节点自动级联）
function syncChecked() {
  nextTick(() => {
    const arr = [...props.modelValue]
    lastChecked.value = new Set(arr)
    treeRef.value?.setCheckedKeys(arr)
    const total = allCodes.value.length
    checkAll.value = total > 0 && arr.length >= total
  })
}
watch(() => props.modelValue, syncChecked, { immediate: true })
watch(() => props.nodes, syncChecked)

// 全选/全不选：批量设置叶子权限项并回传
function onToggleCheckAll(value: string | number | boolean) {
  applyChecked(new Set(value ? allCodes.value : []))
}

// 全部展开/折叠：直接改 el-tree 内部节点的 expanded 状态
function onToggleExpandAll(value: string | number | boolean) {
  const nodes = (treeRef.value as any)?.store?.nodesMap ?? {}
  Object.values(nodes).forEach((node: any) => { node.expanded = !!value })
}

// 勾选变化：leafOnly=true 只取叶子；成对授予（edit⇒view、取消 view⇒摘 edit）后回传
function onCheck() {
  const next = new Set<string>((treeRef.value?.getCheckedKeys(true) as string[]) || [])
  const prev = lastChecked.value
  // 新勾上的 edit → 自动带上成对 view
  for (const c of next) {
    if (!prev.has(c)) {
      const v = pairedView(c)
      if (v) next.add(v)
    }
  }
  // 取消的 view → 自动摘掉成对 edit
  for (const c of prev) {
    if (!next.has(c)) {
      const e = pairedEdit(c)
      if (e && next.has(e)) next.delete(e)
    }
  }
  // 兜底：确保 edit 一定带 view
  for (const c of [...next]) {
    const v = pairedView(c)
    if (v) next.add(v)
  }
  applyChecked(next)
}
</script>

<style scoped>
.pts-toolbar {
  display: flex;
  align-items: center;
  gap: 24px;
  margin-bottom: 10px;
}
.pts-switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.pts-switch-label {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.pts-panel {
  min-height: 120px;
  max-height: 420px;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 4px;
}
.pts-panel :deep(.el-tree-node__label) {
  font-size: 13px;
}
</style>
