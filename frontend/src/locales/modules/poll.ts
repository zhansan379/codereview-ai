// 补拉(usePoll)状态提示:触发失败、后台失败、以及本轮结果汇总。
// 结果汇总写成整句多参数词条(不做分片 join),避免英文语序错乱。
export default {
  'zh-CN': {
    triggerFailed: '补拉触发失败',
    failed: '补拉失败：{error}',
    done: '补拉完成：扫描 {prs} 个 PR/MR，新入队 {queued} 条审查，已审过跳过 {skipped}，审查在后台进行，可在审查记录页查看',
    doneWithErrors: '补拉完成：扫描 {prs} 个 PR/MR，新入队 {queued} 条审查，已审过跳过 {skipped}，失败 {errors}，审查在后台进行，可在审查记录页查看',
  },
  en: {
    triggerFailed: 'Failed to start backfill',
    failed: 'Backfill failed: {error}',
    done: 'Backfill done: scanned {prs} PRs/MRs, queued {queued} new reviews, skipped {skipped} already reviewed. Reviews run in the background — check the review records page.',
    doneWithErrors: 'Backfill done: scanned {prs} PRs/MRs, queued {queued} new reviews, skipped {skipped} already reviewed, {errors} failed. Reviews run in the background — check the review records page.',
  },
}
