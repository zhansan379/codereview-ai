<template>
  <div class="swimlanes">
    <!-- 文件组图例：仅当存在 ≥2 个并行文件组时显示，说明泳道内各卡片的组归属 -->
    <div v-if="showGroups" class="legend">
      <span class="legend-label">{{ $t('swimLane.fileGroup') }}</span>
      <span v-for="g in groupMap.list" :key="g.key" class="legend-chip">
        <span class="gdot" :style="{ background: g.color }" />
        <span class="gfiles" :title="g.key">{{ g.short }}</span>
      </span>
    </div>

    <section
      v-for="lane in lanes"
      :key="lane.phase"
      class="lane"
      :style="{ '--lane': laneColor(lane.phase) }"
    >
      <!-- 泳道头：任务类型标签 + 卡片计数 -->
      <header class="lane-head">
        <span class="lane-dot" />
        <span class="lane-title">{{ lane.label }}</span>
        <code class="lane-code">{{ lane.phase }}</code>
        <span class="lane-count">{{ $t('swimLane.rounds', { n: lane.cards.length }) }}</span>
        <span
          v-if="lane.tokens"
          class="lane-tokens"
          :title="$t('swimLane.tokensTitle', { prompt: lane.tokens.prompt, completion: lane.tokens.completion })"
        >
          {{ $t('swimLane.tokens', { prompt: lane.tokens.prompt, completion: lane.tokens.completion }) }}
        </span>
      </header>

      <!-- 泳道内任务卡片（每张 = 一次 LLM 往返，默认展开） -->
      <div class="lane-cards">
        <article
          v-for="card in lane.cards"
          :key="card.it.seq"
          class="card"
          :class="{ closed: isCollapsed(card) }"
        >
          <header class="card-head" @click="toggle(card)">
            <span class="forehead">{{ isCollapsed(card) ? '▸' : '▾' }}</span>
            <span class="request">Request #{{ card.it.seq }}</span>
            <el-tag size="small" :type="phaseTag(card.it.phase)">{{ phaseLabel(card.it.phase) }}</el-tag>
            <span class="badge badge-model" :title="card.it.model">{{ card.it.model || '—' }}</span>
            <span class="badge badge-time">{{ formatTime(card.it.ts) }}</span>
            <span v-if="groupColor(card)" class="g-chip" :style="{ '--gc': groupColor(card) }" :title="card.it.file_group">
              <span class="gdot" :style="{ background: groupColor(card) }" />
              {{ groupShort(card) }}
            </span>
            <span v-if="usageOf(card)" class="badge badge-tokens">{{
              $t('swimLane.tokens', {
                prompt: usageOf(card).prompt_tokens,
                completion: usageOf(card).completion_tokens,
              })
            }}</span>
            <span v-if="card.it.trace_id" class="trace" title="trace_id"><code>{{ card.it.trace_id }}</code></span>
          </header>

          <div v-if="!isCollapsed(card)" class="card-body">
            <!-- 思考链：参考页「Reasoning」块——thinking 模型的推理过程（content 常为空时它才是主数据） -->
            <div v-if="reasoningOf(card)" class="sec">
              <div class="sec-label">{{ $t('swimLane.reasoning') }}</div>
              <pre class="text reasoning">{{ reasoningOf(card) }}</pre>
            </div>

            <!-- 响应：模型原始输出 -->
            <div v-if="respContent(card) !== null" class="sec">
              <div class="sec-label">{{ $t('swimLane.response') }}</div>
              <div v-if="looksTruncatedJson(respContent(card))" class="warn">
                {{ $t('swimLane.responseTruncated') }}
              </div>
              <pre v-if="respText(card) !== null" class="text">{{ respText(card) }}</pre>
              <JsonView v-else :data="respContent(card)" class="json" />
            </div>

            <!-- 工具调用 + 参数 + 结果（结果取自下一轮 request 里按 id 配对的 tool 消息） -->
            <div v-if="respToolCalls(card).length" class="sec toolcalls-section">
              <div class="sec-label">
                {{ $t('swimLane.toolCalls', { n: respToolCalls(card).length }) }}
              </div>
              <div v-for="(tc, j) in respToolCalls(card)" :key="'tc' + j" class="toolcall">
                <details class="toolcall-detail">
                  <summary class="toolcall-head">
                    <span class="chevron-sm" />
                    <span class="tool-icon">&#9881;</span>
                    <b class="tool-name" :title="tc.id">{{ toolNameOf(tc) }}</b>
                    <span v-if="toolResultFor(card, tc)" class="badge badge-neutral">
                      {{ $t('swimLane.hasResult') }}
                    </span>
                  </summary>
                  <div class="toolcall-body">
                    <div v-if="argsOf(tc)">
                      <div class="tl">{{ $t('swimLane.arguments') }}</div>
                      <JsonView :data="argsOf(tc)" class="json" />
                    </div>
                    <div v-if="toolResultFor(card, tc)">
                      <div class="tl">{{ $t('swimLane.result') }}</div>
                      <div
                        v-if="looksTruncatedJson(toolResultFor(card, tc).content)"
                        class="warn"
                      >{{ $t('swimLane.resultTruncated') }}</div>
                      <pre
                        v-if="toolResultFor(card, tc).content && !isJson(toolResultFor(card, tc).content)"
                        class="text small"
                      >{{ renderMessage(toolResultFor(card, tc)) }}</pre>
                      <JsonView
                        v-else-if="isJson(toolResultFor(card, tc).content)"
                        :data="toolResultFor(card, tc).content"
                        class="json"
                      />
                      <el-empty v-else-if="!toolResultFor(card, tc).content" :description="$t('swimLane.emptyResult')" :image-size="24" />
                    </div>
                  </div>
                </details>
              </div>
            </div>

            <!-- 请求消息（默认折叠）：这轮真正发给模型的完整输入。grouping 卡即喂进去的文件元数据 -->
            <details v-if="requestMessages(card.it).length" class="reqd">
              <summary class="reqd-toggle">
                <span class="chevron-sm" />
                <span>{{ $t('swimLane.requestMessages', { n: requestMessages(card.it).length }) }}</span>
              </summary>
              <div class="reqd-body">
                <div v-for="(m, i) in requestMessages(card.it)" :key="'req' + i" class="msg">
                  <div class="msg-head">
                    <el-tag size="small" :type="roleTag(m.role)">{{ roleLabel(m.role) }}</el-tag>
                    <span v-if="m.name" class="muted name">{{ m.name }}</span>
                    <span v-if="m.tool_calls?.length" class="muted">
                      {{ $t('swimLane.containsToolCalls', { n: m.tool_calls.length }) }}
                    </span>
                  </div>
                  <div v-if="m.content && looksTruncatedJson(m.content)" class="warn">
                    {{ $t('swimLane.requestTruncated') }}
                  </div>
                  <pre v-if="m.content && !isJson(m.content)" class="text small">{{ renderMessage(m) }}</pre>
                  <JsonView v-else-if="isJson(m.content)" :data="m.content" class="json" />
                </div>
              </div>
            </details>

            <el-empty
              v-if="respContent(card) === null && !reasoningOf(card) && !respToolCalls(card).length"
              :description="$t('swimLane.noContent')" :image-size="36"
            />
          </div>
        </article>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * 自定义组件：把一次 review 的 agentic LLM 往返，回放为「任务类型泳道 + 任务卡片」。
 * 对齐 open-code-review 上游 `ocr viewer` 会话详情页的任务卡片：
 *   - 每轮 LLM 往返一张卡，卡片头=请求号/模型/token/时间/trace；
 *   - 卡片体 = 原始**响应** + **工具调用**（参数 + 按 id 从下一轮 request 配对到的结果），
 *     逐条可折叠；不渲染完整 messages（同上游，消息留待 JSONL 转录）。
 * 组件无状态获取，数据由父组件懒加载后传入。
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ConversationItem } from '../api'
import { formatTime } from '../utils/format'
import JsonView from './JsonView.vue'

const props = defineProps<{ items: ConversationItem[] }>()
const { t, te } = useI18n()

// 阶段元数据：el-tag 色 + 泳道色（同型任务同色）。标签文案在 enum.phase.* 里，别重复维护。
const PHASES: Record<string, { tag: string; color: string }> = {
  plan: { tag: 'primary', color: '#7c3aed' },
  grouping: { tag: 'info', color: '#0891b2' },
  main: { tag: '', color: '#4f46e5' },
  re_location: { tag: 'info', color: '#ea580c' },
  review_filter: { tag: 'warning', color: '#ca8a04' },
  scoring: { tag: 'success', color: '#16a34a' },
  compress: { tag: 'info', color: '#8b949e' },
  loop: { tag: '', color: '#64748b' },
}
const PHASE_ORDER = Object.keys(PHASES)

function phaseLabel(p: string): string {
  // 后端若新增阶段，没有词条时回退显示原始值
  return te(`enum.phase.${p}`) ? t(`enum.phase.${p}`) : p
}
function phaseTag(p: string): any {
  return PHASES[p]?.tag ?? 'info'
}
function laneColor(p: string): string {
  return PHASES[p]?.color ?? '#57606a'
}

// ── 数据归一 ──────────────────────────────────────────────

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

function roleLabel(r: string): string {
  return te(`swimLane.role.${r}`) ? t(`swimLane.role.${r}`) : r || t('swimLane.role.unknown')
}
function roleTag(r: string): any {
  if (r === 'user') return 'primary'
  if (r === 'assistant') return 'success'
  if (r === 'tool') return 'warning'
  if (r === 'system') return 'info'
  return 'info'
}

/** tool_call 的函数名：兼容嵌套(chat-completions `function.name`)与扁平(`name`)两种形状。 */
function toolNameOf(tc: any): string {
  return tc.function?.name ?? tc.name ?? ''
}

/** tool_call 的参数：兼容嵌套(`function.arguments`)与扁平(`arguments`/`args`/`raw_arguments`)；
 *  原生可能是对象或 JSON 字符串，交给 JsonView 归一化渲染。 */
function argsOf(tc: any): any {
  return tc.function?.arguments ?? tc.arguments ?? tc.args ?? tc.raw_arguments ?? ''
}

/** 给定消息集里 tool_call_id → 对应 role='tool' 结果消息。 */
function toolResults(msgs: any[]): Record<string, any> {
  const map: Record<string, any> = {}
  for (const m of msgs) {
    if (m.role === 'tool' && m.tool_call_id) map[m.tool_call_id] = m
  }
  return map
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

/** 形似 JSON 但解析失败（多数是旧数据被写入端 `[:2000]` 切断成残缺 JSON）；
 *  这类给纯文本 + 截断提示，避免 JsonView 折叠崩溃。 */
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

// ── 卡片数据：本轮 = response；其结果落在紧邻下一轮的 request 里，按 id 配对 ──
interface Card {
  it: ConversationItem
  nextMsgs: any[]
}

const cardsData = computed<Card[]>(() =>
  props.items.map((it, idx) => ({
    it,
    nextMsgs: idx + 1 < props.items.length ? requestMessages(props.items[idx + 1]) : [],
  })),
)

// 工具结果：本轮 response 里的 tool_call，去下一轮 request 里找 role='tool' 的同 id 消息。
function toolResultFor(card: Card, tc: any): any {
  return toolResults(card.nextMsgs)[tc.id] ?? null
}

function usageOf(card: Card): any {
  return card.it.response?.usage
}
function reasoningOf(card: Card): string {
  return card.it.response?.reasoning_content ?? ''
}
function respContent(card: Card): any {
  return card.it.response?.content ?? null
}
function respText(card: Card): string | null {
  const c = respContent(card)
  if (c == null) return null
  if (typeof c === 'string') return isJson(c) ? null : c
  return renderMessage({ content: c })
}
function respToolCalls(card: Card): any[] {
  return card.it.response?.tool_calls ?? []
}

// ── 文件组归属：组审查并行时逐卡带 `file_group`（排序路径逗号连接；空 = 整组/未分组）──
interface GroupInfo {
  key: string
  short: string
  color: string
}
// 组配色盘：与泳道色区分，标识并行组，借组序号循环取色。
const GROUP_COLORS = ['#dc2626', '#0891b2', '#ca8a04', '#7c3aed', '#16a34a', '#ea580c', '#2563eb', '#be185d']

/** 组 key 的短标签：单文件显示路径，多文件「首个路径 +N」。 */
function groupShortLabel(key: string): string {
  const parts = key.split(',')
  if (parts.length <= 1) return key
  return `${parts[0]} +${parts.length - 1}`
}

const groupMap = computed(() => {
  const seen = new Set<string>()
  const list: GroupInfo[] = []
  for (const it of props.items) {
    const key = it.file_group?.trim()
    if (!key || seen.has(key)) continue
    seen.add(key)
    list.push({ key, short: groupShortLabel(key), color: GROUP_COLORS[list.length % GROUP_COLORS.length] })
  }
  return { list, byKey: new Map(list.map((g) => [g.key, g])) }
})

/** 存在 ≥2 个不同文件组才显示组归属（单组/整组审查无区可区分，保持干净）。 */
const showGroups = computed(() => groupMap.value.list.length >= 2)

function groupColor(card: Card): string {
  if (!showGroups.value) return ''
  return groupMap.value.byKey.get(card.it.file_group?.trim() || '')?.color ?? ''
}
function groupShort(card: Card): string {
  const key = card.it.file_group?.trim() || ''
  return groupMap.value.byKey.get(key)?.short ?? key
}

// ── 泳道分组：按阶段分组，规范顺序排列，只保留出现的阶段 ──
const lanes = computed(() => {
  const byPhase = new Map<string, Card[]>()
  for (const c of cardsData.value) {
    const list = byPhase.get(c.it.phase) ?? []
    list.push(c)
    byPhase.set(c.it.phase, list)
  }
  const present = Array.from(byPhase.keys())
  return PHASE_ORDER.filter((p) => byPhase.has(p))
    .concat(present.filter((p) => !PHASE_ORDER.includes(p)))
    .map((phase) => {
      const cards = byPhase.get(phase)!.sort((a, b) => a.it.seq - b.it.seq)
      const tokens = cards.reduce<{ prompt: number; completion: number } | null>((acc, c) => {
        const u = usageOf(c)
        if (!u) return acc
        acc ??= { prompt: 0, completion: 0 }
        acc.prompt += u.prompt_tokens ?? 0
        acc.completion += u.completion_tokens ?? 0
        return acc
      }, null)
      return { phase, label: phaseLabel(phase), cards, tokens }
    })
})

// ── 卡片折叠：默认全部展开；点卡片头切换 ──
const collapsed = ref<Set<number>>(new Set())
function isCollapsed(card: Card): boolean {
  return collapsed.value.has(card.it.seq)
}
function toggle(card: Card): void {
  const next = new Set(collapsed.value)
  if (next.has(card.it.seq)) next.delete(card.it.seq)
  else next.add(card.it.seq)
  collapsed.value = next
}
</script>

<style scoped>
/* ── 泳道布局 ── */
.swimlanes {
  display: flex;
  flex-direction: column;
  gap: 20px;
  margin-top: 16px;
}
.legend {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--border, var(--el-border-color-light));
  border-radius: 8px;
  background: var(--el-bg-color);
}
.legend-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin-right: 2px;
}
.legend-chip, .g-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid color-mix(in srgb, var(--gc, var(--el-text-color-secondary)) 30%, transparent);
  background: color-mix(in srgb, var(--gc, var(--el-text-color-secondary)) 6%, var(--el-bg-color));
  color: var(--el-text-color-primary);
  padding: 2px 9px;
  border-radius: 20px;
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
  font-size: 11px;
  max-width: 220px;
}
.gfiles {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gdot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.g-chip {
  padding-top: 1px;
  padding-bottom: 1px;
}
.lane {
  border: 1px solid var(--border, var(--el-border-color-light));
  border-left: 3px solid var(--lane);
  border-radius: 10px;
  background: var(--surface, var(--el-bg-color));
  overflow: hidden;
}
.lane-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border, var(--el-border-color-light));
  background: color-mix(in srgb, var(--lane) 6%, var(--el-bg-color));
}
.lane-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--lane);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--lane) 18%, transparent);
  flex-shrink: 0;
}
.lane-title {
  font-weight: 600;
  font-size: 14px;
  color: var(--el-text-color-primary);
  letter-spacing: -0.01em;
}
.lane-code {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
}
.lane-count {
  margin-left: auto;
  font-size: 12px;
  color: var(--lane);
  font-weight: 600;
  background: color-mix(in srgb, var(--lane) 12%, transparent);
  padding: 2px 10px;
  border-radius: 20px;
}
.lane-tokens {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
}
.lane-cards {
  padding: 12px 16px 8px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* ── 任务卡片 ── */
.card {
  border: 1px solid var(--border, var(--el-border-color-light));
  border-radius: 8px;
  overflow: hidden;
  transition: box-shadow 0.15s ease;
  background: var(--el-bg-color);
}
.card:hover {
  box-shadow: 0 2px 8px rgba(15, 17, 24, 0.06);
}
.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--el-fill-color-light);
  border-bottom: 1px solid var(--border, var(--el-border-color-light));
  cursor: pointer;
  flex-wrap: wrap;
  user-select: none;
}
.card-head:hover {
  background: color-mix(in srgb, var(--lane) 7%, var(--el-fill-color-light));
}
.forehead {
  color: var(--el-text-color-secondary);
  font-size: 11px;
  width: 12px;
}
.request {
  font-weight: 600;
  font-size: 12px;
  color: var(--el-text-color-primary);
}
.badge {
  padding: 2px 9px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.5;
  white-space: nowrap;
}
.badge-model {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
}
.badge-time {
  background: var(--el-fill-color);
  color: var(--el-text-color-regular);
}
.badge-tokens {
  background: var(--el-color-success-light-9);
  color: var(--el-color-success);
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
}
.badge-neutral {
  background: var(--el-fill-color);
  color: var(--el-text-color-regular);
}
.trace {
  margin-left: auto;
  font-size: 11px;
  color: var(--el-text-color-secondary);
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace code {
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
}

/* ── 卡片体 ── */
.card-body {
  padding: 6px 14px 12px;
}
.sec {
  padding: 8px 0 2px;
}
.sec-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--el-text-color-secondary);
  margin: 8px 0 6px;
}
.warn {
  margin: 2px 0 8px;
  padding: 4px 10px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 4px;
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning);
  font-size: 12px;
}
.text {
  margin: 0;
  padding: 8px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-lighter);
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}
.text.small {
  font-size: 12px;
  background: var(--el-bg-color);
}
.text.reasoning {
  background: var(--el-fill-color-lighter);
  border-color: var(--el-border-color-light);
  max-height: 480px;
  color: var(--el-text-color-regular);
}
.json {
  padding: 8px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-lighter);
}

/* ── 工具调用 ── */
.toolcalls-section {
  margin-top: 4px;
}
.toolcall {
  margin-bottom: 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  overflow: hidden;
}
.toolcall-detail {
  list-style: none;
}
.toolcall-detail::-webkit-details-marker {
  display: none;
}
.toolcall-detail::marker {
  content: '';
}
.toolcall-detail[open] > .toolcall-head .chevron-sm {
  transform: rotate(45deg);
}
.toolcall-head {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 10px;
  font-size: 12px;
  user-select: none;
}
.toolcall-head:hover {
  background: var(--el-fill-color-light);
}
.toolcall-body {
  padding: 0 10px 10px;
}
.tool-icon {
  color: var(--el-text-color-placeholder);
}
.tool-name {
  color: #7c3aed;
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
  font-weight: 500;
}
.chevron-sm {
  display: inline-block;
  width: 5px;
  height: 5px;
  border-right: 1.5px solid var(--el-text-color-secondary);
  border-bottom: 1.5px solid var(--el-text-color-secondary);
  transform: rotate(-45deg);
  transition: transform 0.15s ease;
  flex-shrink: 0;
}
.tl {
  font-size: 11px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin: 10px 0 5px;
}

/* ── 请求消息（默认折叠）── */
.reqd {
  margin-top: 10px;
  border-top: 1px dashed var(--el-border-color-lighter);
}
.reqd-toggle {
  cursor: pointer;
  padding: 9px 0 4px;
  display: flex;
  align-items: center;
  gap: 8px;
  list-style: none;
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-regular);
  user-select: none;
}
.reqd-toggle::-webkit-details-marker,
summary::-webkit-details-marker {
  display: none;
}
.reqd-toggle::marker {
  content: '';
}
.reqd[open] > .reqd-toggle .chevron-sm {
  transform: rotate(45deg);
}
.reqd-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 6px 0 4px;
}
.msg {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.msg-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.name {
  font-size: 12px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>