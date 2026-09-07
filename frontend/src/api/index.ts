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
}

export interface ForgeConfig {
  provider: string
  url: string
  token: string
  env_active: boolean
  enabled: boolean
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
}

// ===== 认证 =====
export function login(password: string): Promise<LoginResult> {
  return client.post('/auth/login', { password }).then((r) => r.data)
}

// ===== 项目 =====
export function listProjects(): Promise<Project[]> {
  return client.get('/projects').then((r) => r.data)
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
export function testForge(provider: string, data?: { url?: string; token?: string }): Promise<{ ok: boolean }> {
  return client.post(`/forges/${provider}/test`, data || {}).then((r) => r.data)
}

// ===== 通知渠道 =====
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
export function listReviews(params: { state?: string; limit?: number; offset?: number }): Promise<ReviewList> {
  return client.get('/reviews', { params }).then((r) => r.data)
}
export function getReview(id: number): Promise<ReviewDetail> {
  return client.get(`/reviews/${id}`).then((r) => r.data)
}
export function setFindingStatus(id: number, status: 'waived' | 'active'): Promise<ReviewFinding> {
  return client.post(`/reviews/findings/${id}/status`, { status }).then((r) => r.data)
}

// ===== 任务 =====
export function listTasks(params: { state?: string }): Promise<TaskItem[]> {
  return client.get('/tasks', { params }).then((r) => r.data)
}
export function retryTask(id: number): Promise<{ id: number; state: string; attempt: number }> {
  return client.post(`/tasks/${id}/retry`).then((r) => r.data)
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
  provider_split: CountItem[]
}
export function getStats(): Promise<DashboardStats> {
  return client.get('/stats').then((r) => r.data)
}