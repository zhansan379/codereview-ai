// 枚举值 → 展示标签。key 与后端返回的原始值一一对应;
// 查不到词条时调用方回退显示原始值(后端新增枚举不会显示成空白)。
//
// 注意:仪表盘「任务状态分布」用的是另一套措辞(已完成/跳过),在 dashboard 模块里,
// 这里保持审查记录列表/详情页的措辞(审查成功/已跳过),不要合并。
export default {
  'zh-CN': {
    state: {
      queued: '排队中',
      running: '运行中',
      completed: '审查成功',
      success: '成功',
      reviewed: '已审',
      failed: '失败',
      skipped: '已跳过',
    },
    severity: {
      critical: '严重',
      high: '高',
      medium: '中',
      low: '低',
    },
    mode: {
      agentic: 'Agent',
      diff: 'Diff',
      notRun: '未执行',
    },
    findingStatus: {
      active: '待处理',
      resolved: '已解决',
    },
    phase: {
      plan: '规划',
      grouping: '文件分组',
      main: '主循环',
      re_location: '行号重锚',
      review_filter: '复现核查',
      scoring: '评分',
      compress: '压缩',
      loop: '后台循环',
    },
    bucket: {
      new: '新增',
      persisting: '持续存在',
      resolved: '已解决',
      not_reviewed: '上次未覆盖',
    },
  },
  en: {
    state: {
      queued: 'Queued',
      running: 'Running',
      completed: 'Completed',
      success: 'Success',
      reviewed: 'Reviewed',
      failed: 'Failed',
      skipped: 'Skipped',
    },
    severity: {
      critical: 'Critical',
      high: 'High',
      medium: 'Medium',
      low: 'Low',
    },
    mode: {
      agentic: 'Agent',
      diff: 'Diff',
      notRun: 'Not run',
    },
    findingStatus: {
      active: 'Open',
      resolved: 'Resolved',
    },
    phase: {
      plan: 'Plan',
      grouping: 'File grouping',
      main: 'Main loop',
      re_location: 'Line re-anchor',
      review_filter: 'Reproduction check',
      scoring: 'Scoring',
      compress: 'Compress',
      loop: 'Background loop',
    },
    bucket: {
      new: 'New',
      persisting: 'Persisting',
      resolved: 'Resolved',
      not_reviewed: 'Not covered last round',
    },
  },
}
