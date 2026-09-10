// 原始对话页(ConversationView):页头、空态说明、分页加载按钮。
export default {
  'zh-CN': {
    pageTitle: '原始对话 #{id}',
    emptyDesc: '该任务无原始 LLM 对话（仅 agentic 多轮采集 plan/main/re_location/review_filter/scoring/compress；diff 轨不采集）',
    loadMore: '加载更多（已显示 {shown} / {total}）',
  },
  en: {
    pageTitle: 'Raw conversation #{id}',
    emptyDesc: 'No raw LLM conversation for this task (only agentic runs record plan/main/re_location/review_filter/scoring/compress; the diff track records none)',
    loadMore: 'Load more ({shown} / {total} shown)',
  },
}
