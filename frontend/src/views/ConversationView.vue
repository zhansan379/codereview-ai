<template>
  <div>
    <el-page-header @back="$router.back()" :content="`原始对话 #${id}`" />

    <el-empty
      v-if="!loading && items.length === 0"
      description="该任务无原始 LLM 对话（仅 agentic 多轮采集 plan/main/re_location/review_filter/scoring/compress；diff 轨不采集）"
    />

    <div v-loading="loading" class="timeline">
      <el-card v-for="t in turns" :key="t.it.seq" class="turn" :body-style="{ padding: '14px 20px' }">
        <template #header>
          <div class="turn-head">
            <el-tag :type="phaseTag(t.it.phase)" size="small">{{ phaseLabel(t.it.phase) }}</el-tag>
            <span class="muted">#{{ t.it.seq }}</span>
            <span class="muted model">{{ t.it.model || '—' }}</span>
            <span class="muted">{{ formatTime(t.it.ts) }}</span>
            <span v-if="t.it.trace_id" class="muted trace"><code>{{ t.it.trace_id }}</code></span>
          </div>
        </template>

        <div v-if="turnMessages(t).length" class="section-label">请求（messages）</div>
        <template v-for="(m, i) in turnMessages(t)" :key="'req' + i">
          <div v-if="!isInlinedTool(turnMessages(t), m)" class="msg">
            <div class="msg-role">
              <el-tag size="small" :type="roleTag(m.role)">{{ roleLabel(m.role) }}</el-tag>
              <span v-if="m.name" class="muted name">{{ m.name }}</span>
            </div>
            <div v-if="m.content && looksTruncatedJson(m.content)" class="truncated-warn">
              ⚠ 该请求内容是被旧版写入端 `[:2000]` 切断的残缺 JSON，无法按结构折叠
            </div>
            <pre v-if="m.content && !isJson(m.content)" class="msg-body">{{ renderMessage(m) }}</pre>
            <JsonView v-else-if="isJson(m.content)" :data="m.content" class="msg-json" />
            <div v-if="m.tool_calls?.length" class="tool-calls">
              <details v-for="(tc, j) in m.tool_calls" :key="'tc' + j">
                <summary>
                  <el-tag size="small" type="info">tool_call</el-tag> <b>{{ toolNameOf(tc) }}</b> <span class="muted">{{ tc.id }}</span>
                </summary>
                <div v-if="argsOf(tc)" class="tc-label">参数</div>
                <JsonView v-if="argsOf(tc)" :data="argsOf(tc)" class="msg-json" />
                <div v-if="toolResult(turnMessages(t), tc)" class="tc-label">结果</div>
                <template v-if="toolResult(turnMessages(t), tc)">
                  <div v-if="looksTruncatedJson(toolResult(turnMessages(t), tc).content)" class="truncated-warn">
                    ⚠ 工具结果 JSON 被写入端截断，无法按结构折叠
                  </div>
                  <pre v-if="toolResult(turnMessages(t), tc).content && !isJson(toolResult(turnMessages(t), tc).content)" class="msg-body small">{{ renderMessage(toolResult(turnMessages(t), tc)) }}</pre>
                  <JsonView v-else-if="isJson(toolResult(turnMessages(t), tc).content)" :data="toolResult(turnMessages(t), tc).content" class="msg-json" />
                </template>
              </details>
            </div>
          </div>
        </template>

        <div v-if="t.isDelta" class="ctx-note">
          <span class="muted">
            {{ turnExpanded(t)
                ? '已展开完整上下文'
                : `本轮仅新增 ${t.delta.length} 条消息（上下文共 ${requestMessages(t.it).length} 条已折叠）` }}
          </span>
          <el-button link type="primary" size="small" @click="toggleContext(t)">
            {{ turnExpanded(t) ? '收起为新增' : '展开完整上下文' }}
          </el-button>
        </div>

        <div class="section-label">响应</div>
        <div v-if="t.it.response?.content && looksTruncatedJson(t.it.response.content)" class="truncated-warn">
          ⚠ 响应 JSON 被写入端截断，无法按结构折叠
        </div>
        <pre v-if="t.it.response?.content && !isJson(t.it.response.content)" class="msg-body">{{ t.it.response.content }}</pre>
        <JsonView v-else-if="t.it.response?.content && isJson(t.it.response.content)" :data="t.it.response.content" class="msg-json" />
        <el-empty v-if="!t.it.response?.content && !t.it.response?.tool_calls?.length" description="无文本内容" :image-size="36" />
        <div v-if="t.it.response?.tool_calls?.length" class="tool-calls">
          <div v-for="(tc, j) in t.it.response.tool_calls" :key="'r' + j" class="tool-call">
            <div class="msg-role">
              <el-tag size="small" type="warning">tool_call</el-tag> <b>{{ toolNameOf(tc) }}</b> <span class="muted">{{ tc.id }}</span>
            </div>
            <JsonView v-if="argsOf(tc)" :data="argsOf(tc)" class="msg-json" />
          </div>
        </div>
        <div v-if="t.it.response?.usage" class="usage muted">
          输入 {{ t.it.response.usage.prompt_tokens }} · 输出 {{ t.it.response.usage.completion_tokens }} · 总 {{ t.it.response.usage.total_tokens }}
        </div>
      </el-card>
    </div>

    <div v-if="hasMore && !loading" class="more">
      <el-button :loading="loadingMore" @click="loadMore">
        加载更多（已显示 {{ items.length }} / {{ total }}）
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { fetchReviewConversation, type ConversationItem, type ConversationPage } from '../api'
import { formatTime } from '../utils/format'
import JsonView from '../components/JsonView.vue'

const route = useRoute()
const id = Number(route.params.id)
const items = ref<ConversationItem[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const total = ref(0)
const hasMore = ref(false)
// 分页懒加载：每页取 PAGE 条，翻页追加到 items；offset 指向下一页起点。
const PAGE = 30

const PHASES: Record<string, { label: string; tag: string }> = {
  plan: { label: '规划', tag: 'primary' },
  main: { label: '主循环', tag: '' },
  re_location: { label: '行号重锚', tag: 'info' },
  review_filter: { label: '复现核查', tag: 'warning' },
  scoring: { label: '评分', tag: 'success' },
  compress: { label: '压缩', tag: 'info' },
  loop: { label: '后台循环', tag: '' },
}

function phaseLabel(p: string): string {
  return PHASES[p]?.label ?? p
}
function phaseTag(p: string): any {
  return PHASES[p]?.tag ?? 'info'
}

function roleLabel(r: string): string {
  if (r === 'user') return '用户'
  if (r === 'assistant') return '模型'
  if (r === 'tool') return '工具结果'
  return r || '未知'
}
function roleTag(r: string): any {
  if (r === 'user') return 'primary'
  if (r === 'assistant') return 'success'
  if (r === 'tool') return 'warning'
  return 'info'
}

// request 可能存为「消息数组」或「{ messages }」两种内嵌形态，归一成数组。
function requestMessages(it: ConversationItem): any[] {
  if (Array.isArray(it.request)) return it.request
  if (it.request && Array.isArray(it.request.messages)) return it.request.messages
  return []
}

function renderMessage(m: any): string {
  const c = m && m.content
  if (typeof c === 'string') return c
  if (Array.isArray(c)) {
    return c.map((blk: any) => (typeof blk === 'string' ? blk : blk.text ?? '')).filter(Boolean).join('\n')
  }
  return ''
}

/** tool_call 的函数名：兼容嵌套(chat-completions `function.name`)与扁平(`name`)两种形状。 */
function toolNameOf(tc: any): string {
  return tc.function?.name ?? tc.name ?? ''
}

/** tool_call 的参数：兼容嵌套(`function.arguments`)与扁平(`arguments`/`args`)两形状；
 *  原生可能是对象或 JSON 字符串，交给 JsonView 归一化渲染。 */
function argsOf(tc: any): any {
  return tc.function?.arguments ?? tc.arguments ?? tc.args ?? tc.raw_arguments ?? ''
}

/** 给定消息集里 tool_call_id → 对应 role='tool' 结果消息（供折叠面板内联配对）。 */
function toolResults(msgs: any[]): Record<string, any> {
  const map: Record<string, any> = {}
  for (const m of msgs) {
    if (m.role === 'tool' && m.tool_call_id) map[m.tool_call_id] = m
  }
  return map
}

/** 该 tool_call 在给定消息集里是否有可配对的结果消息。 */
function toolResult(msgs: any[], tc: any): any {
  return toolResults(msgs)[tc.id] ?? null
}

/** tool 结果消息已在对应 tool_call 面板内联展示，则跳过独立渲染，避免重复。 */
function isInlinedTool(msgs: any[], m: any): boolean {
  return m.role === 'tool' && m.tool_call_id ? toolResults(msgs)[m.tool_call_id] !== undefined : false
}

/** 结构化内容等值比较（agentic 历史逐轮累积，用前缀比对口决增量）。 */
function deepEq(a: any, b: any): boolean {
  if (a === b) return true
  try {
    return JSON.stringify(a) === JSON.stringify(b)
  } catch {
    return false
  }
}

/** 一个轮次 = 相对上一轮**新增**的消息；若上一轮非本轮前缀（换阶段/新循环）则整体显示。 */
interface Turn {
  it: ConversationItem
  delta: any[]
  isDelta: boolean
}

function messageDelta(it: ConversationItem, prev: ConversationItem | null): { msgs: any[]; isDelta: boolean } {
  const cur = requestMessages(it)
  if (!prev) return { msgs: cur, isDelta: false }
  const prevMsgs = requestMessages(prev)
  const k = Math.min(prevMsgs.length, cur.length)
  let same = 0
  while (same < k && deepEq(prevMsgs[same], cur[same])) same++
  if (same === prevMsgs.length && same <= cur.length) {
    return { msgs: cur.slice(same), isDelta: true }
  }
  return { msgs: cur, isDelta: false }
}

const turns = computed<Turn[]>(() =>
  items.value.map((it, idx) => {
    const prev = idx > 0 ? items.value[idx - 1] : null
    const r = messageDelta(it, prev)
    return { it, delta: r.msgs, isDelta: r.isDelta }
  }),
)

// 单轮「展开完整上下文」开关（默认折叠历史，只看新增）。
const expanded = ref<Set<number>>(new Set())
function toggleContext(t: Turn): void {
  const next = new Set(expanded.value)
  if (next.has(t.it.seq)) next.delete(t.it.seq)
  else next.add(t.it.seq)
  expanded.value = next
}
function turnMessages(t: Turn): any[] {
  return expanded.value.has(t.it.seq) ? requestMessages(t.it) : t.delta
}
function turnExpanded(t: Turn): boolean {
  return expanded.value.has(t.it.seq)
}

/** content/参数 是否为纯 JSON 对象/数组字符串（决定是否走 JsonView 折叠渲染）。 */
function isJson(data: any): boolean {
  if (typeof data !== 'string') return false
  const t = data.trim()
  if (!(t.startsWith('{') || t.startsWith('['))) return false
  try {
    const p = JSON.parse(t)
    return p !== null && typeof p === 'object'
  } catch {
    return false
  }
}

/** 形似 JSON 对象/数组、但解析失败（多半是历史数据被写入端 `[:2000]` 切断成残缺 JSON）。
 *  这类内容不给 JsonView（会崩折叠），改成纯文本 + 截断提示。 */
function looksTruncatedJson(data: any): boolean {
  if (typeof data !== 'string') return false
  const t = data.trim()
  if (!(t.startsWith('{') || t.startsWith('['))) return false
  try {
    JSON.parse(t)
    return false
  } catch {
    return true
  }
}

function applyPage(page: ConversationPage) {
  items.value = items.value.concat(page.items)
  total.value = page.total
  hasMore.value = page.has_more
}

async function load() {
  loading.value = true
  try {
    const page = await fetchReviewConversation(id, 0, PAGE)
    applyPage(page)
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  if (loadingMore.value) return
  loadingMore.value = true
  try {
    const page = await fetchReviewConversation(id, items.value.length, PAGE)
    applyPage(page)
  } finally {
    loadingMore.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 16px;
}
.more {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}
.turn-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.muted {
  color: #909399;
  font-size: 12px;
}
.model {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace {
  margin-left: auto;
}
.section-label {
  font-size: 12px;
  color: #909399;
  margin: 6px 0 6px;
  font-weight: 600;
}
.msg {
  margin-bottom: 10px;
}
.msg-role {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.name {
  font-size: 12px;
}
.msg-body {
  margin: 0;
  padding: 8px 12px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #fafafa;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}
.msg-body.small {
  font-size: 12px;
  background: #fff;
}
.truncated-warn {
  margin: 2px 0 6px;
  padding: 4px 8px;
  border: 1px solid #f3d19e;
  border-radius: 4px;
  background: #fdf6ec;
  color: #b88230;
  font-size: 12px;
}
.tool-calls {
  margin: 6px 0 0 24px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.msg-json {
  margin: 6px 0 0 24px;
  padding: 8px 12px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #fafafa;
}
.tc-label {
  font-size: 12px;
  color: #909399;
  margin: 6px 0 4px;
  font-weight: 600;
}
.tool-call {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.usage {
  margin-top: 8px;
}
.ctx-note {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 4px 0 8px;
}
</style>