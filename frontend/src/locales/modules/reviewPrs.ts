// MR/PR 聚合进展页（ReviewPrsView）
// 四桶标签复用 enum.bucket.*，这里只放本页文案与各桶空态提示。
export default {
  'zh-CN': {
    title: 'MR 审查进展',
    search: '查询',
    provider: '平台',
    allProviders: '全部平台',
    prNumber: 'MR/PR 号',
    prNumberPlaceholder: '精确 MR/PR 号',
    keyword: '标题关键词',
    keywordPlaceholder: '匹配 MR/PR 标题',
    finishedAt: '完成时间',
    dateStart: '开始',
    dateEnd: '结束',
    empty: '暂无已完成审查的 MR（需 ≥1 轮 completed 的 mr 任务）',
    noTitle: '（无标题）',
    roundsCount: '共 {n} 轮',
    convergence: '收敛 {pct}%',
    convergenceTitle: '收敛率 {pct}%',
    round: '第{n}轮',
    roundTitle:
      '第{n}轮 {sha}\n+新增 {new} · 持续 {persisting} · 已解决 {resolved} · 未覆盖 {notReviewed}',
    notReviewedAlertTitle: '存在上轮未覆盖（未验证）的问题',
    notReviewedAlertDesc:
      '这些问题上次报过、但本轮未审到对应文件（未变更复用/缺失覆盖集时保守登记）。≠ 已修复，需注意。',
    emptyBucket: {
      new: '本轮无新增问题',
      persisting: '无持续存在的问题',
      resolved: '本轮已全部修复',
      not_reviewed: '无',
    },
  },
  en: {
    title: 'MR review progress',
    search: 'Search',
    provider: 'Platform',
    allProviders: 'All platforms',
    prNumber: 'MR/PR number',
    prNumberPlaceholder: 'Exact MR/PR number',
    keyword: 'Title keyword',
    keywordPlaceholder: 'Match MR/PR title',
    finishedAt: 'Finished at',
    dateStart: 'Start',
    dateEnd: 'End',
    empty: 'No reviewed MR yet (needs at least one completed mr task)',
    noTitle: '(untitled)',
    roundsCount: '{n} rounds',
    convergence: 'converged {pct}%',
    convergenceTitle: 'Convergence rate {pct}%',
    round: 'Round {n}',
    roundTitle:
      'Round {n} {sha}\n+new {new} · persisting {persisting} · resolved {resolved} · not covered {notReviewed}',
    notReviewedAlertTitle: 'Some findings were not covered (unverified) this round',
    notReviewedAlertDesc:
      'These were reported before, but their files were not reviewed this round (conservatively recorded when files are unchanged or the coverage set is missing). This is NOT "fixed" — take a look.',
    emptyBucket: {
      new: 'No new findings this round',
      persisting: 'No persisting findings',
      resolved: 'Everything fixed this round',
      not_reviewed: 'None',
    },
  },
}
