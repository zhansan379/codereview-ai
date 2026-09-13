import client from './client'

// ===== 成员分析（/stats/workrate/members）=====
// 独立模块：api/index.ts 体积已大，且避免与其余在途改动相互牵连。

export interface MemberStat {
  name: string
  projects: number
  commits: number
  daily_avg: number
  /** 本地日期 → 提交次数（仅含有提交的日期） */
  trend: Record<string, number>
  /** 平均评分；无已审任务时为 null */
  avg_score: number | null
  /** 计入评分的任务数 */
  scored: number
  /** 增/删行（新任务有精确值；存量行 null → 前端退化显示 changed_lines） */
  additions: number | null
  deletions: number | null
  /** 增删合计行数（diff_lines 口径，旧数据兜底展示用） */
  changed_lines: number
}

export interface MembersReport {
  scope: {
    from: string
    to: string
    commits: number
    members: number
  }
  members: MemberStat[]
}

export function getMembersReport(params: {
  date_from?: string
  date_to?: string
  tz?: number
}): Promise<MembersReport> {
  return client.get('/stats/workrate/members', { params }).then((r) => r.data)
}
