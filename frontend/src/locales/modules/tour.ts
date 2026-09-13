// Tour 漫游引导：步骤标题/描述 + 顶栏帮助按钮的 tooltip。
// 按钮文案（上一步/下一步/完成）用 Element Plus 自带语言包，不在这里重复。
export default {
  'zh-CN': {
    help: '本页引导',
    start: '开始体验',
    welcomeTitle: '欢迎使用 CodeReview AI',
    welcomeDesc: '这是一套 AI 代码审查平台。跟随本引导快速了解各功能区，随时可按 Esc 或右上角 × 退出。',
    menuTitle: '功能导航',
    menuDesc: '所有功能模块都从左侧菜单进入，按「分析 / 审查 / 资源 / 系统」分组展示，菜单项按你的权限显示。',
    kpiTitle: '核心指标',
    kpiDesc: '任务总数、问题总数、未修复的高危/严重问题与平均对话轮数一目了然。',
    bandsTitle: '分布概览',
    bandsDesc: '按严重度、状态、模型供应商与审查模式拆分的数字分布，快速掌握问题构成。',
    trendTitle: '趋势与成本图表',
    trendDesc: '审查趋势、成本、token 消耗、耗时与阶段分布等图表，悬停可查看数值明细。',
    recentTitle: '最近审查',
    recentDesc: '最近完成的审查任务，点击行即可进入审查详情查看问题与对话。',
    themeTitle: '外观切换',
    themeDesc: '在亮色 / 暗色主题间切换，图表配色会跟随主题自动重绘。',
    langTitle: '语言切换',
    langDesc: '界面支持中文与 English，切换即时生效并记住偏好。',
    userTitle: '账户菜单',
    userDesc: '显示当前用户与角色，可在此退出登录。',
    helpTitle: '每个页面都有专属引导',
    helpDesc: '进入任意主菜单页面后，点顶栏「?」只看该页的功能介绍，不会再重复这些全局内容。',
    noTourTitle: '本页没有专属引导',
    noTourDesc: '这里是详情/子页面。从左侧菜单进入各功能页面后，顶栏「?」可查看对应引导。',
    reviewsTabsTitle: '双视图入口',
    reviewsTabsDesc:
      '「MR 汇总」按 MR/PR 聚合展示审查轨迹；「审查明细」逐条列出每次审查任务。',
    reviewsMrFilterTitle: 'MR 筛选',
    reviewsMrFilterDesc:
      '按平台、PR 号、关键字、完成时间组合筛选；筛选条件会记到地址栏，刷新和分享链接都不丢。',
    reviewsMrListTitle: 'MR 聚合卡片',
    reviewsMrListDesc:
      '每张卡片是一个 MR/PR 的审查轨迹：轮次时间线 + 末轮四桶明细（新增 / 持续存在 / 已解决 / 上次未覆盖）。',
    reviewsDetailFilterTitle: '审查筛选与导出',
    reviewsDetailFilterDesc:
      '支持状态、平台、事件类型、评分区间与完成时间组合筛选；右上角可按当前条件导出 Excel。',
    reviewsDetailTableTitle: '审查列表',
    reviewsDetailTableDesc:
      '每行是一次审查任务，可进入详情、重试、重新投递或删除；底部为服务端分页。',
    projectsToolbarTitle: '新增与补拉',
    projectsToolbarDesc:
      '「新增项目」手动登记仓库；「补拉 PR/MR」从平台自动同步项目列表（已存在自动跳过）。',
    projectsTableTitle: '项目列表',
    projectsTableDesc:
      '「审查策略」列区分 diff / agentic 审查方式；行内可管理项目成员、编辑或删除。',
    modelsToolbarTitle: '接入模型',
    modelsToolbarDesc:
      '「新增模型」登记 LLM 供应商：名称、Base URL、密钥与优先级（数字越小越优先调用）。',
    modelsTableTitle: '模型列表',
    modelsTableDesc: '「测试」一键验证连通性；模型可随时编辑或删除，启停即时生效。',
    notifiersToolbarTitle: '通知渠道',
    notifiersToolbarDesc:
      '「新增通知渠道」接入 IM Webhook；「{\'@\'}成员管理」维护通知名单，支持按项目覆盖或全局生效。',
    notifiersTableTitle: '渠道列表',
    notifiersTableDesc:
      '每行一个 Webhook 通道：作用范围、{\'@\'} 全体、启停一目了然，行内可编辑或删除。',
    schedulesToolbarTitle: '定时任务',
    schedulesToolbarDesc:
      '「新增定时任务」按周期自动执行，类型有主动补拉轮询和日报；worker 离线时这里会有提示。',
    schedulesTableTitle: '任务列表',
    schedulesTableDesc: '「立即运行」手动触发一次；行内可编辑参数、启停或删除任务。',
    cachesPolicyTitle: '清除策略',
    cachesPolicyDesc:
      '控制 agentic 拉取缓存的自动清理：开关与保留天数，改完记得保存，也可立即清理一次。',
    cachesListTitle: '拉取记录',
    cachesListDesc:
      '列出最近的缓存拉取信息；「扫描存量目录」可把磁盘上已有缓存登记进来复用。',
    settingsForgeTitle: '平台接入',
    settingsForgeDesc:
      '配置 GitHub / GitLab / Gitee 的 URL 与 Token（加密入库）；环境变量优先于页面配置，保存后热更生效，可一键测试连通性。',
    settingsConcurrencyTitle: '审查并发',
    settingsConcurrencyDesc: '控制同时进行的审查数量，避免打满机器或触发平台限流。',
    settingsAutoTitle: '自动审查触发',
    settingsAutoDesc: '按事件（push / MR）自动触发审查的总开关与默认行为，项目级可覆盖。',
    settingsSecurityTitle: '安全说明',
    settingsSecurityDesc: 'Webhook 签名、登录令牌与 Token 存储方式的说明，排障时先看这里。',
    usersToolbarTitle: '新增用户',
    usersToolbarDesc: '创建账号并设置初始密码，可分配角色。',
    usersTableTitle: '用户列表',
    usersTableDesc: '分配角色、启用/停用、重置密码；操作即时生效。',
    rolesToolbarTitle: '新增角色',
    rolesToolbarDesc: '创建自定义角色，再通过行内「权限」入口配置权限集。',
    rolesTableTitle: '角色列表',
    rolesTableDesc:
      '内置角色不可删改；「权限」入口查看/调整权限集，成员数实时统计。',
  },
  en: {
    help: 'Page tour',
    start: "Let's start",
    welcomeTitle: 'Welcome to CodeReview AI',
    welcomeDesc:
      'This is an AI code review platform. Follow this quick tour to explore the workspace — press Esc or click × anytime to exit.',
    menuTitle: 'Navigation',
    menuDesc:
      'All modules live in the sidebar, grouped into Analytics / Review / Resources / System; menu items appear based on your permissions.',
    kpiTitle: 'Key metrics',
    kpiDesc: 'Total tasks, findings, unresolved high/critical issues and average chat rounds at a glance.',
    bandsTitle: 'Distribution overview',
    bandsDesc: 'Findings broken down by severity, state, model provider and review mode.',
    trendTitle: 'Trend & cost charts',
    trendDesc: 'Review trend, cost, token usage, duration and phase charts — hover for details.',
    recentTitle: 'Recent reviews',
    recentDesc: 'Recently finished reviews. Click a row to open the review detail with findings and conversation.',
    themeTitle: 'Appearance',
    themeDesc: 'Switch between light and dark themes; chart colors follow automatically.',
    langTitle: 'Language',
    langDesc: 'The UI speaks 中文 and English. Switching applies instantly and is remembered.',
    userTitle: 'Account menu',
    userDesc: 'Shows the current user and role; sign out from here.',
    helpTitle: 'Every page has its own tour',
    helpDesc:
      'On any page, click the "?" in the header for that page\'s focused tour — these global steps won\'t repeat.',
    noTourTitle: 'No dedicated tour here',
    noTourDesc:
      'This is a detail/sub page. Open a module from the sidebar, then click the "?" in its header for a tour.',
    reviewsTabsTitle: 'Two views, one page',
    reviewsTabsDesc:
      '"MR summary" aggregates review rounds per merge request; "Detail" lists every review task one by one.',
    reviewsMrFilterTitle: 'Filter MRs',
    reviewsMrFilterDesc:
      'Combine provider, PR number, keyword and finish date; conditions are kept in the URL, so refresh and shared links survive.',
    reviewsMrListTitle: 'MR cards',
    reviewsMrListDesc:
      'Each card is one MR/PR review track: a round timeline plus the last round\'s four buckets (new / persisting / resolved / not covered).',
    reviewsDetailFilterTitle: 'Filter & export',
    reviewsDetailFilterDesc:
      'Combine state, provider, event type, score range and finish date; export the current result to Excel from the top-right.',
    reviewsDetailTableTitle: 'Review list',
    reviewsDetailTableDesc:
      'One review task per row — open details, retry, redeliver or delete; server-side pagination below.',
    projectsToolbarTitle: 'Create & backfill',
    projectsToolbarDesc:
      '"New project" registers a repository manually; "Backfill PR/MR" syncs the list from your platform (existing ones are skipped).',
    projectsTableTitle: 'Project list',
    projectsTableDesc:
      'The strategy column tells diff from agentic review; manage members, edit or delete a project per row.',
    modelsToolbarTitle: 'Add a model',
    modelsToolbarDesc:
      '"Add model" registers an LLM provider: name, base URL, API key and priority (lower number is tried first).',
    modelsTableTitle: 'Model list',
    modelsTableDesc: '"Test" checks connectivity in one click; edit or remove models anytime.',
    notifiersToolbarTitle: 'Notification channels',
    notifiersToolbarDesc:
      '"New notifier" hooks up an IM webhook; "Member management" maintains the mention list, globally or per project.',
    notifiersTableTitle: 'Channel list',
    notifiersTableDesc:
      'One webhook channel per row: scope, {\'@\'}-all and enabled state at a glance; edit or delete inline.',
    schedulesToolbarTitle: 'Scheduled jobs',
    schedulesToolbarDesc:
      '"New scheduled job" runs tasks on a schedule — backfill polling or daily reports; a banner warns when the worker is offline.',
    schedulesTableTitle: 'Job list',
    schedulesTableDesc: '"Run now" triggers once manually; edit params, enable/disable or delete per row.',
    cachesPolicyTitle: 'Prune policy',
    cachesPolicyDesc:
      'Controls auto-cleanup of agentic clone caches: switch and retention days — remember to save, or prune now once.',
    cachesListTitle: 'Recent fetches',
    cachesListDesc:
      'Lists recent cache fetches; "Scan existing dirs" registers caches already on disk for reuse.',
    settingsForgeTitle: 'Platform access',
    settingsForgeDesc:
      'Configure GitHub / GitLab / Gitee URL and tokens (encrypted at rest); env vars take priority, changes hot-reload, one-click connectivity test.',
    settingsConcurrencyTitle: 'Review concurrency',
    settingsConcurrencyDesc: 'Caps how many reviews run at once to avoid saturating the machine or rate limits.',
    settingsAutoTitle: 'Automatic triggers',
    settingsAutoDesc: 'Global switch and defaults for event-triggered reviews (push / MR); projects can override.',
    settingsSecurityTitle: 'Security notes',
    settingsSecurityDesc: 'How webhook signatures, session tokens and stored tokens work — start here when troubleshooting.',
    usersToolbarTitle: 'Add a user',
    usersToolbarDesc: 'Create an account with an initial password and assign a role.',
    usersTableTitle: 'User list',
    usersTableDesc: 'Assign roles, enable/disable, reset passwords — all effective immediately.',
    rolesToolbarTitle: 'Add a role',
    rolesToolbarDesc: 'Create a custom role, then set its permissions via the inline "Permissions" entry.',
    rolesTableTitle: 'Role list',
    rolesTableDesc:
      'Built-in roles cannot be edited or deleted; the "Permissions" entry shows/edits the permission set, with live member counts.',
  },
}
