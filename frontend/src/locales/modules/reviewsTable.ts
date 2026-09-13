// 审查记录 / 仪表盘「最近记录」共用表格(components/ReviewsTable.vue)的列头与行内按钮。
export default {
  'zh-CN': {
    title: '标题',
    author: '作者',
    provider: '平台',
    repoId: '仓库 ID',
    event: '事件',
    branch: '分支',
    attempt: '重试',
    score: '评分',
    mode: '模式',
    state: '状态',
    queuedAt: '排队时间',
    finishedAt: '完成时间',
    retry: '重试',
    // 「执行」：未开始（门控跳过/未配置 LLM/手动停止）的任务手动开跑一次，
    // 会绕过项目审查开关强制审；failed 走「重试」语义。
    execute: '执行',
    stop: '停止',
    resend: '重新发送',
  },
  en: {
    title: 'Title',
    author: 'Author',
    provider: 'Provider',
    repoId: 'Repo ID',
    event: 'Event',
    branch: 'Branch',
    attempt: 'Retries',
    score: 'Score',
    mode: 'Mode',
    state: 'Status',
    queuedAt: 'Queued at',
    finishedAt: 'Finished at',
    retry: 'Retry',
    // 「Run」：kick off a not-started task once (bypasses project switches)
    execute: 'Run',
    stop: 'Stop',
    resend: 'Resend',
  },
}
