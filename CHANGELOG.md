# 2026-05-12 改动记录

## 已完成

### 服务端预载（核心改进）
- `page_investment` 路由现在直接从 SQLite 查询全部数据，序列化为 JSON 嵌入 HTML 的 `window._preload`
- 页面首屏 0 个 API 请求，全部数据随 HTML 一起到达
- 包含：账户、交易记录、定投计划、月度余额、持仓汇总（成本法）、汇率

### 前端渲染
- `renderPreloaded(P)` 函数从预载数据直接渲染所有卡片/表格/DCA
- 页面打开即显示，不等待任何 API 响应
- `startAutoRefresh` 不再在 init 时调用 loadPortfolio（避免覆盖预载渲染）

### 外部调用清理
- 移除 Yahoo Finance 搜索（`autoFillName` 不再调外部 API）
- `loadPerformanceChart` 仅在刷新行情时触发
- 汇率刷新仅在「刷新行情」按钮点击时执行

### 多版本对比
- `investment.html` (v1) - 主力版本，预载 + 完整交互
- `investment2.html` (v2) - 旧版备份，纯 API 驱动
- `investment3.html` (v3) - 极简静态版，仅展示无交互

### 其他
- `REQUIREMENTS.md` - 功能清单文档
- `TODO.md` - 待优化清单
- `CHANGELOG.md` - 本文件
- Git 分支: `preload-rewrite`

## 未完成/待优化

### 饼图颜色
- 当前使用双色板配色，用户不满意
- 六色方案：#427AB2 #F09148 #FF9896 #DBDB8D #C59D94 #AFC7E8
- 九色方案：#43978F #9EC4BE #ABD0F1 #DCE9F4 #E56F5E #F19685 #F6C957 #FFB77F #FBE8D5

### 收益率计算
- 折线图使用成本基准法 `(市值/成本 - 1) × 100%`
- 连续买入时成本基准变动导致曲线不平滑
- 考虑 TWR 或修正 Dietz

### 账户余额计算
- 投资账户余额来自月度手动填写
- 与自动计算的关系不清晰

### 总览页面数字
- 总资产 = 非投资余额 + 投资组合市值
- 口径需要进一步对齐

### linter/formatter 干扰
- 有个外部工具持续自动修改 investment.html 的 JS 代码
- 导致函数名被改写、调用链断裂
- 未知来源，无配置文件（.prettier/.editorconfig 均不存在）
- 疑似 Claude Code 系统内部机制
