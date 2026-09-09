import client from './client'

// 所有 REST 返回类型的 interface 定义（与后端 JSON 对应）

export interface User {
  id: number | string
  username: string
  [key: string]: any
}

export interface LoginResult {
  access_token: string
  token_type: string
  expires_in: number
  user: User
}

export interface Project {
  id: number
  provider: string
  repo_id: string
  repo_full_name: string
  web_url: string
  branch_rule: string | null
  file_extensions: string | null
  review_strategy: string | null
  prompt_suffix: string | null
  score_threshold: number | null
  enforce_score_threshold: boolean
  notifier_routing: Record<string, any> | null
  enabled: boolean
  push_enabled: boolean | null
  push_branch_globs: string | null
  mr_enabled: boolean | null
}

export interface ModelItem {
  id: number
  name: string
  provider: string
  model: string
  api_key: string
  base_url: string | null
  temperature: number | null
  max_tokens: number | null
  capabilities: string[] | null
  priority: number | null
  enabled: boolean
}

export interface Notifier {
  id: number
  channel: 'dingtalk' | 'feishu' | 'wecom'
  enabled: boolean
  webhook: string
  secret: string
  project_id: number | null
  at_threshold: number | null
  at_all: boolean
  at_member_ids: number[]
}

export interface NotifierMember {
  id: number
  name: string
  git_username: string
  dingtalk_mobile: string
  wecom_userid: string
  feishu_open_id: string
}

export interface ForgeConfig {
  provider: string
  url: string
  token: string
  env_active: boolean
  enabled: boolean
}

export interface ForgeCapability {
  name: string
  label: string
  status: 'ok' | 'missing' | 'unknown'
  detail: string
}

export interface ForgeProbeResult {
  ok: boolean
  capabilities: ForgeCapability[]
}

export interface ReviewFinding {
  id: number
  severity: string
  category: string
  file: string
  old_line: number | null
  new_line: number | null
  title: string
  detail: string | null
  existing_code: string | null
  suggestion: string | null
  source: string | null
  status: string | null
  first_seen: string | null
  last_seen: string | null
  reopened_count: number | null
}

export interface ReviewItem {
  id: number
  provider: string
  repo_id: string
  pr_number: number | null
  pr_title: string | null
  web_url: string | null
  push_commits: string | null
  event_type: string | null
  branch: string | null
  head_sha: string | null
  base_sha: string | null
  state: string
  attempt: number
  error: string | null
  skip_reason: string | null
  trace_id: string | null
  summary_md: string | null
  score_total: number | null
  queued_at: string | null
  finished_at: string | null
  writeback_failed: boolean | null
}

export interface ReviewDetail extends ReviewItem {
  findings: ReviewFinding[]
}

export interface ReviewList {
  items: ReviewItem[]
  total: number
  limit: number
  offset: number
}

export interface TaskItem {
  id: number
  provider: string
  repo_id: string
  pr_number: number | null
  event_type: string | null
  branch: string | null
  head_sha: string | null
  state: string
  attempt: number
  error: string | null
  skip_reason: string | null
  queued_at: string | null
  writeback_failed: boolean | null
}

// ===== 认证 =====
export function login(password: string): Promise<LoginResult> {
  return client.post('/auth/login', { password }).then((r) => r.data)
}

// ===== 项目 =====
export interface ResolvedRepo {
  repo_id: string
  repo_full_name: string
  web_url: string
}

export function listProjects(): Promise<Project[]> {
  return client.get('/projects').then((r) => r.data)
}
export function resolveRepo(data: { provider: string; url: string }): Promise<ResolvedRepo> {
  return client.post('/forges/resolve-repo', data).then((r) => r.data)
}
export function createProject(data: Partial<Project>): Promise<Project> {
  return client.post('/projects', data).then((r) => r.data)
}
export function updateProject(id: number, data: Partial<Project>): Promise<Project> {
  return client.put(`/projects/${id}`, data).then((r) => r.data)
}
export function deleteProject(id: number): Promise<any> {
  return client.delete(`/projects/${id}`).then((r) => r.data)
}

// ===== 模型 =====
export function listModels(): Promise<ModelItem[]> {
  return client.get('/models').then((r) => r.data)
}
export function createModel(data: Partial<ModelItem>): Promise<ModelItem> {
  return client.post('/models', data).then((r) => r.data)
}
export function updateModel(id: number, data: Partial<ModelItem>): Promise<ModelItem> {
  return client.put(`/models/${id}`, data).then((r) => r.data)
}
export function deleteModel(id: number): Promise<any> {
  return client.delete(`/models/${id}`).then((r) => r.data)
}
export function testModel(id: number, prompt?: string): Promise<any> {
  return client.post(`/models/${id}/test`, { prompt }).then((r) => r.data)
}

// ===== 平台接入（GitHub/GitLab token+url）=====
export function listForges(): Promise<ForgeConfig[]> {
  return client.get('/forges').then((r) => r.data)
}
export function updateForge(provider: string, data: Partial<ForgeConfig>): Promise<ForgeConfig> {
  return client.put(`/forges/${provider}`, data).then((r) => r.data)
}
export function testForge(provider: string, data?: { url?: string; token?: string }): Promise<ForgeProbeResult> {
  return client.post(`/forges/${provider}/test`, data || {}).then((r) => r.data)
}

// ===== 定时任务（主动补拉轮询 / 日报）=====
export interface ScheduleJob {
  id: number
  name: string
  job_type: 'poll' | 'daily'
  enabled: boolean
  params: { interval_seconds?: number; hour?: number }
}
export interface ScheduleList {
  items: ScheduleJob[]
  worker_active: boolean
}
export function listSchedules(): Promise<ScheduleList> {
  return client.get('/schedules').then((r) => r.data)
}
export function createSchedule(data: Partial<ScheduleJob>): Promise<ScheduleJob> {
  return client.post('/schedules', data).then((r) => r.data)
}
export function updateSchedule(id: number, data: Partial<ScheduleJob>): Promise<ScheduleJob> {
  return client.put(`/schedules/${id}`, data).then((r) => r.data)
}
export function deleteSchedule(id: number): Promise<any> {
  return client.delete(`/schedules/${id}`).then((r) => r.data)
}
export function runSchedule(id: number): Promise<any> {
  return client.post(`/schedules/${id}/run`).then((r) => r.data)
}

// ===== agent 本地克隆缓存（拉取缓存）=====
export interface CloneCacheItem {
  id: number
  repo_key: string
  provider: string
  repo_full_name: string
  url: string
  local_path: string
  head_sha: string
  last_error: string
  created_at: string
  last_fetched_at: string
}
export interface CloneCacheSettings {
  enabled: boolean
  days: number
}
export interface CloneCacheList {
  cache_root: string
  enabled: boolean
  days: number
  pruner_running: boolean
  items: CloneCacheItem[]
}
export function listCloneCaches(): Promise<CloneCacheList> {
  return client.get('/agent/caches').then((r) => r.data)
}
export function deleteCloneCache(id: number): Promise<any> {
  return client.delete(`/agent/caches/${id}`).then((r) => r.data)
}
export function getCloneCacheSettings(): Promise<CloneCacheSettings> {
  return client.get('/agent/caches/settings').then((r) => r.data)
}
export function updateCloneCacheSettings(data: CloneCacheSettings): Promise<CloneCacheSettings> {
  return client.put('/agent/caches/settings', data).then((r) => r.data)
}
export function pruneCloneCaches(): Promise<{ deleted: string[]; count: number }> {
  return client.post('/agent/caches/prune').then((r) => r.data)
}
export function rebuildCloneCaches(): Promise<{ added: number; cache_root: string }> {
  return client.post('/agent/caches/rebuild').then((r) => r.data)
}

// ===== 全局运行时设置（审查并发）=====
export interface ConcurrencySetting {
  concurrency: number
  active: boolean
  applied: boolean
}
export function getConcurrency(): Promise<ConcurrencySetting> {
  return client.get('/settings/concurrency').then((r) => r.data)
}
export function setConcurrency(data: { concurrency: number }): Promise<ConcurrencySetting> {
  return client.post('/settings/concurrency', data).then((r) => r.data)
}

// ===== push 自动审查默认开关（§7.7 全局默认）=====
export interface PushReviewDefaultSetting {
  enabled: boolean
  source: 'db' | 'env'
}
export function getPushReviewDefault(): Promise<PushReviewDefaultSetting> {
  return client.get('/settings/push-review-default').then((r) => r.data)
}
export function setPushReviewDefault(data: { enabled: boolean }): Promise<PushReviewDefaultSetting> {
  return client.post('/settings/push-review-default', data).then((r) => r.data)
}

// ===== MR 自动审查默认开关（与 push 对称，全局默认）=====
export interface MrReviewDefaultSetting {
  enabled: boolean
  source: 'db' | 'env'
}
export function getMrReviewDefault(): Promise<MrReviewDefaultSetting> {
  return client.get('/settings/mr-review-default').then((r) => r.data)
}
export function setMrReviewDefault(data: { enabled: boolean }): Promise<MrReviewDefaultSetting> {
  return client.post('/settings/mr-review-default', data).then((r) => r.data)
}

// ===== 主动补拉 PR/MR =====
export interface PollReport {
  projects: number
  prs: number
  new: number
  skipped: number
  errors: string[]
}
export interface PollProgress {
  done: number
  total: number
  new: number
  skipped: number
}
export interface PollStatus {
  running: boolean
  report: PollReport | null
  error: string | null
  /** 进行中逐条进度（仅 running 时有意义） */
  progress?: PollProgress | null
}
export function pollPulls(): Promise<PollStatus> {
  return client.post('/pulls/poll').then((r) => r.data)
}
export function pollPullStatus(): Promise<PollStatus> {
  return client.get('/pulls/poll/status').then((r) => r.data)
}

// ===== 通知渠道 =====
export function listMembers(): Promise<NotifierMember[]> {
  return client.get('/notifiers/members').then((r) => r.data)
}
export function createMember(data: Partial<NotifierMember>): Promise<NotifierMember> {
  return client.post('/notifiers/members', data).then((r) => r.data)
}
export function updateMember(id: number, data: Partial<NotifierMember>): Promise<NotifierMember> {
  return client.put(`/notifiers/members/${id}`, data).then((r) => r.data)
}
export function deleteMember(id: number): Promise<any> {
  return client.delete(`/notifiers/members/${id}`).then((r) => r.data)
}

export function listNotifiers(): Promise<Notifier[]> {
  return client.get('/notifiers').then((r) => r.data)
}
export function createNotifier(data: Partial<Notifier>): Promise<Notifier> {
  return client.post('/notifiers', data).then((r) => r.data)
}
export function updateNotifier(id: number, data: Partial<Notifier>): Promise<Notifier> {
  return client.put(`/notifiers/${id}`, data).then((r) => r.data)
}
export function deleteNotifier(id: number): Promise<any> {
  return client.delete(`/notifiers/${id}`).then((r) => r.data)
}

// ===== 审查记录 =====
export interface ReviewFilter {
  state?: string
  event_type?: string
  provider?: string
  score_min?: number
  score_max?: number
  finished_from?: string
  finished_to?: string
  limit?: number
  offset?: number
}
export function listReviews(params: ReviewFilter): Promise<ReviewList> {
  return client.get('/reviews', { params }).then((r) => r.data)
}
export function getReview(id: number): Promise<ReviewDetail> {
  return client.get(`/reviews/${id}`).then((r) => r.data)
}
export function deleteReview(id: number): Promise<any> {
  return client.delete(`/reviews/${id}`).then((r) => r.data)
}
export function setFindingStatus(id: number, status: 'waived' | 'active'): Promise<ReviewFinding> {
  return client.post(`/reviews/findings/${id}/status`, { status }).then((r) => r.data)
}

// ===== agentic 对话 / 前后对比 =====
export interface ConversationItem {
  seq: number
  phase: string
  model: string
  trace_id: string
  request: any
  response: any
  ts: string
}

export interface ConversationPage {
  items: ConversationItem[]
  offset: number
  limit: number
  total: number
  has_more: boolean
}

// compare 四桶：为后端 `bucket_compare` 平铺的 finding dict。
export interface CompareBucketItem {
  content: string
  category: string
  severity: string
  file: string
  line: number | null
  old_line: number | null
  existing_code: string
  source: string
}

export interface CompareResult {
  new: CompareBucketItem[]
  persisting: CompareBucketItem[]
  resolved: CompareBucketItem[]
  not_reviewed: CompareBucketItem[]
}

export function fetchReviewConversation(
  id: number,
  offset = 0,
  limit = 30,
): Promise<ConversationPage> {
  return client
    .get(`/reviews/${id}/conversation`, { params: { offset, limit } })
    .then((r) => r.data)
}
export function fetchReviewCompare(id: number): Promise<CompareResult> {
  return client.get(`/reviews/${id}/compare`).then((r) => r.data)
}
// Excel 导出：以 blob 请求，返回下载文件原始字节。顶层筛选与列表一致（所见即所导）。
export function exportReviews(
  params: ReviewFilter & { severities?: string[]; statuses?: string[] },
): Promise<Blob> {
  return client
    .get('/reviews/export', { params, responseType: 'blob' })
    .then((r) => r.data as Blob)
}

// ===== 任务 =====
export function listTasks(params: { state?: string }): Promise<TaskItem[]> {
  return client.get('/tasks', { params }).then((r) => r.data)
}
export function retryTask(id: number): Promise<{ id: number; state: string; attempt: number }> {
  return client.post(`/tasks/${id}/retry`).then((r) => r.data)
}

export function redeliverTask(id: number): Promise<{ id: number; status: string }> {
  return client.post(`/tasks/${id}/redeliver`).then((r) => r.data)
}

// ===== 看板统计 =====
export interface CountItem {
  key: string
  count: number
}
export interface ReviewsByDay {
  day: string
  count: number
}
export interface ModelUsageItem {
  model: string
  requests: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost: number
}
export interface DailyCostItem {
  day: string
  cost: number
}
export interface DailyDurationItem {
  day: string
  count: number
  avg_seconds: number
}
export interface AgentScatterItem {
  diff_lines: number
  duration_s: number
  chat_rounds: number
  tool_calls: number
}
export interface DashboardStats {
  total_tasks: number
  total_findings: number
  open_critical: number
  open_high: number
  tasks_by_state: CountItem[]
  findings_by_severity: CountItem[]
  findings_by_category: CountItem[]
  reviews_by_day: ReviewsByDay[]
  model_usage: ModelUsageItem[]
  cost_by_day: DailyCostItem[]
  duration_by_day: DailyDurationItem[]
  phase_dist: CountItem[]
  provider_split: CountItem[]
  tasks_by_mode: CountItem[]
  agent_task_count: number
  avg_chat_rounds: number
  agent_scatter: AgentScatterItem[]
}
export function getStats(mode: 'all' | 'agentic' | 'diff' = 'all'): Promise<DashboardStats> {
  return client.get('/stats', { params: { mode } }).then((r) => r.data)
}