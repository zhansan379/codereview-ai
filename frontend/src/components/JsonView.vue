<template>
  <div class="jnode">
    <template v-if="meta.container">
      <div class="jrow clickable" @click="open = !open">
        <span class="caret">{{ open ? '▾' : '▸' }}</span>
        <span v-if="meta.label !== null" class="jkey">{{ meta.label }}:</span>
        <span class="jop">{{ meta.openBracket }}</span>
        <span v-if="!open" class="jpreview">…</span>
        <span v-if="!open" class="jop">{{ meta.closeBracket }}</span>
      </div>
      <div v-if="open" class="jblock">
        <JsonView
          v-for="(child, i) in meta.children"
          :key="`${meta.style}-${i}`"
          :data="child.value"
          :label="child.label"
          :depth="(depth ?? 0) + 1"
        />
        <div class="jrow">
          <span class="jop">{{ meta.closeBracket }}</span>
        </div>
      </div>
    </template>
    <div v-else class="jrow">
      <span v-if="meta.label !== null" class="jkey">{{ meta.label }}:</span>
      <span v-if="meta.long" class="jval str text">{{ meta.text }}</span>
      <span v-else class="jval" :class="meta.style">{{ meta.text }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 递归 JSON 查看器：对象/数组可折叠，按类型着色；字符串里若是合法 JSON 对象/数组，
 * 自动展开成可折叠结构（覆盖 tool 参数常以 JSON 字符串入库的情形）。
 * 自递归：组件内用自身文件名 `<JsonView>`。
 */
import { computed, ref, toRefs } from 'vue'

const props = defineProps<{
  data: any
  /** 键名（对象条目）/ 数组下标；顶层为 null */
  label?: string | number | null
  /** 当前深度，用于默认折叠阈值 */
  depth?: number
}>()

// 默认展开层级：深度在阈值内默认展开，更深默认折叠。
const open = ref((props.depth ?? 0) < 2)
const { data } = toRefs(props)

interface Child {
  label: string | number | null
  value: any
}

function parseContainer(v: any): any {
  // 字符串若为纯 JSON 对象/数组 → 解析为结构；其余原样。
  if (typeof v === 'string') {
    const t = v.trim()
    if (t.startsWith('{') && t.endsWith('}')) {
      try {
        const p = JSON.parse(t)
        if (p && typeof p === 'object' && !Array.isArray(p)) return p
      } catch { /* 保持字符串 */ }
    } else if (t.startsWith('[') && t.endsWith(']')) {
      try {
        const p = JSON.parse(t)
        if (Array.isArray(p)) return p
      } catch { /* 保持字符串 */ }
    }
    return v
  }
  return v
}

const node = computed(() => parseContainer(data.value))

const meta = computed(() => {
  const v = node.value
  const label = props.label === undefined ? null : props.label
  if (Array.isArray(v)) {
    const children: Child[] = v.map((x: any, i: number) => ({ label: i, value: x }))
    return { container: true, style: 'array', openBracket: '[', closeBracket: ']', children }
  }
  if (v !== null && typeof v === 'object') {
    const children: Child[] = Object.entries(v).map(([k, val]) => ({ label: k, value: val }))
    return { container: true, style: 'object', openBracket: '{', closeBracket: '}', children }
  }
  // 标量
  if (v === null) {
    return { container: false, style: 'null', text: 'null', long: false, label }
  }
  if (typeof v === 'string') {
    const long = v.length > 80 || v.includes('\n')
    return { container: false, style: 'str', text: long ? v : JSON.stringify(v), long, label }
  }
  if (typeof v === 'number') return { container: false, style: 'num', text: String(v), long: false, label }
  if (typeof v === 'boolean') return { container: false, style: 'bool', text: String(v), long: false, label }
  return { container: false, style: 'null', text: String(v), long: false, label }
})
</script>

<style scoped>
.jnode {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12px;
  line-height: 1.6;
}
.jrow {
  display: flex;
  align-items: baseline;
  gap: 4px;
  white-space: pre-wrap;
  word-break: break-word;
  padding-left: 2px;
}
.jrow.clickable {
  cursor: pointer;
}
.jrow.clickable:hover {
  background: var(--el-color-primary-light-9);
}
.caret {
  color: var(--el-text-color-secondary);
  width: 10px;
  font-size: 11px;
  flex: none;
}
.jblock {
  margin-left: 16px;
  border-left: 1px dashed var(--el-border-color);
  padding-left: 8px;
}
.jkey {
  color: #7a55c9;
  flex: none;
}
.jop {
  color: var(--el-text-color-secondary);
}
.jpreview {
  color: var(--el-text-color-secondary);
}
.jval.num {
  color: #2f6f9f;
}
.jval.str {
  color: #2e7d32;
}
.jval.bool {
  color: #8e24aa;
}
.jval.null {
  color: #9e9e9e;
  font-style: italic;
}
.jval.text {
  display: block;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 语法高亮是分类色（不走 EP 语义 token），暗色下原色太深，单独提亮同色系 */
html.dark .jkey {
  color: #c4a7f5;
}
html.dark .jval.num {
  color: #7fb8e0;
}
html.dark .jval.str {
  color: #7bc47f;
}
html.dark .jval.bool {
  color: #d78ae8;
}
</style>