# TODO — 2026-05-12

## 🔴 CRITICAL（运行时报错/功能不可用）

### 1. backfill 路由装饰器挂错函数，/api/dca-plans/{plan_id}/backfill 完全不可用
- **文件**: `main.py:1044-1048, 1163`
- **问题**: `@app.post("/api/dca-plans/{plan_id}/backfill")`（第1044行）下方紧跟着 `def _resolve_market(asset_type: str)`（3行辅助函数），这个辅助函数变成了路由处理器。真正的实现 `def api_dca_backfill(...)`（第1163行）没有路由装饰器，永远不会被调用。
- **修法**: 删除第1044行的 `@app.post(...)` 装饰器，把装饰器移到第1163行 `def api_dca_backfill(...)` 上方。

### 2. backtest.py: DCA禁用时 NameError 崩溃
- **文件**: `backtest.py:366, 669`
- **问题**: `dca_records` 只在 `if dca.get("enabled"):` 块内定义（第366行）。DCA禁用时变量未定义，但第571行（`combined_cost`）、第669行（返回字典）、第691-696行（summary）无条件引用它，运行即崩溃。
- **修法**: 在 `if dca.get("enabled"):` 之前（约第319行）初始化 `dca_records = []`。

### 3. investment_backup.html: catchUpDca 循环永不执行
- **文件**: `templates/investment_backup.html:168-185`
- **问题**: `while` 条件包含 `safety < 30`，但 `safety` 未声明。`safety++` 对 `undefined` 产生 `NaN`，`undefined < 30` 为 `false`，循环体一次都不执行。
- **修法**: 在 for 循环前（第168行）加 `var safety = 0;`。

---

## 🟠 HIGH（功能缺陷/逻辑错误）

### 4. main.py: 重复的 @app.get("/api/investments") 装饰器
- **文件**: `main.py:1359, 1362`
- **问题**: 第1359行有一个孤立的 `@app.get("/api/investments")`（下方无函数体），紧接着第1362行又来一个。造成路由注册异常。
- **修法**: 删除第1359行的孤立装饰器。

### 5. expenses.html: recNotes 元素缺失，定期支出备注永远存不上
- **文件**: `templates/expenses.html:232, 54-60`
- **问题**: `saveRecurring()` 第232行读取 `document.getElementById('recNotes').value`，但表单（第54-60行）中没有 id="recNotes" 的 input 元素。备注字段静默丢失。
- **修法**: 在定期支出的表单里加 `<input id="recNotes" placeholder="备注">`。

### 6. DCA 创建/更新绕过 Pydantic 校验
- **文件**: `main.py:910-913, 926-935`
- **问题**: `api_dca_create` 和 `api_dca_update` 接受 `data: dict`，不做任何字段校验。其他所有 CRUD 都用 Pydantic schema。
- **修法**: 在 `schemas.py` 加 `DcaPlanCreate` / `DcaPlanUpdate`，路由签名改用 Pydantic 模型。

### 7. backtest.html: rapidOpts/dcaOpts 内联样式冲突
- **文件**: `templates/backtest.html:23, 31`
- **问题**: `style="display:none;display:flex;"` — 后面的 `display:flex` 覆盖了 `display:none`，元素始终可见，`toggleRapid()`/`toggleDca()` 切换失效。
- **修法**: 改为 `style="display:flex;"` 并在元素渲染后调用 toggle 函数设初始状态。

### 8. 备份导入无事务保护，部分导入后失败会脏数据
- **文件**: `main.py:2419-2475`（备份导入）
- **问题**: `_import_rows` 在循环内逐表 `db.commit()`。如果第三个表失败，前两个表已提交无法回滚，数据库处于半导入状态。
- **修法**: 整个导入包在一个事务里，全部成功再 commit；或者每个表 try/except 并显式 rollback。

---

## 🟡 MEDIUM（代码质量/可维护性）

### 9. _now() 重复定义3次
- **文件**: `main.py:35`, `models.py:7`, `exchange_rate.py:32`
- **问题**: 三处一模一样的 `_now()` 函数。
- **修法**: 在 `database.py` 或新建 `utils.py` 定义一次，各处 import。

### 10. typeLabels/typeColors 在4个模板中重复定义
- **文件**: `templates/base.html`, `accounts.html`, `account_records.html`, `investment3.html`
- **问题**: 相同的资产类型映射对象在4个模板里各写一份。
- **修法**: 在 `base.html` 顶层 script 定义 `window.typeLabels` / `window.typeColors`，其他页面引用即可。

### 11. init_db 用原始 sqlite3 连接，路径可能和 SQLAlchemy 不一致
- **文件**: `database.py:26`
- **问题**: `sqlite3.connect("ledger.db")` 用相对路径，SQLAlchemy 引擎也是 `./ledger.db`。从不同目录启动时可能指向不同文件。
- **修法**: 从 `SQLALCHEMY_DATABASE_URL` 提取路径，或用绝对路径。

### 12. 高频查询列缺少索引
- **文件**: `models.py`
- **问题**: `InvestmentRecord.date`、`ExpenseRecord.datetime`、`IncomeRecord.year/month`、`MonthlyBalance.year/month` 频繁出现在 WHERE 条件中，无索引。数据量上千后查询逐渐变慢。
- **修法**: 给这些列加 `Index`。

### 13. yfinance 同步调用阻塞 async 事件循环
- **文件**: `main.py:1653-1660`（api_portfolio_performance）
- **问题**: `refresh=true` 时，循环内同步调用 `yf.Ticker(...).history(...)`，阻塞整个 async 事件循环，其他请求全部卡住。
- **修法**: 用 `run_in_executor` 扔进线程池。

### 14. 启动时 refresh_all_rates 同步阻塞
- **文件**: `main.py:53`
- **问题**: 如果 frankfurter.dev 不可达，每个币种超时10秒 × 5个币种 = 50秒，应用无法启动。
- **修法**: 放到后台线程执行，或加总超时。

### 15. 多处 fetch 无 try/catch，网络失败静默吞掉
- **文件**: 所有模板的 inline JS
- **问题**: 大量 `await fetch(...)` 没有 `.catch()` 或 try/catch，网络错误时 UI 卡在空状态，用户看不到任何提示。
- **修法**: 统一包装 fetch 调用，失败时 `toast('加载失败', 'err')`。

### 16. api_dca_execute 参数类型写法不规范
- **文件**: `main.py:958`
- **问题**: `price: float | None = None` 作为查询参数，部分 FastAPI 版本可能解析异常。
- **修法**: 改为 `price: Optional[float] = Query(default=None)`。

### 17. _is_market_open 在调用点之后才定义
- **文件**: `main.py:1160, 1430`
- **问题**: `api_market_status`（第1160行）调用了 `_is_market_open()`，但该函数定义在第1430行。虽然 Python 运行时能解析，但不利于阅读和重构。
- **修法**: 把 `_is_market_open()` 移到 `api_market_status` 之前。

### 18. api_stats_investment 检查 dca_buy 类型但从未产生
- **文件**: `main.py:2239, 999`
- **问题**: `api_stats_investment` 过滤 `type == "dca_buy"`，但 DCA 执行时创建的是 `type = "buy"`（第999行），`dca_buy` 类型永远查不到。
- **修法**: 删除 `dca_buy` 检查，或把 DCA 执行改为 `type = "dca_buy"`。

### 19. investment_backup.html 是 investment.html 的几乎完整副本
- **文件**: `templates/investment_backup.html` (~951行)
- **问题**: 与 `investment.html` 约90%重复，DCA 表单缺 `payment_account` 字段。每改一个 bug 要改两个文件。
- **修法**: 考虑合并或抽公共 JS。

---

## 🟢 LOW（小修小补）

### 20. CSS 重复属性
- **文件**: `static/style.css:153`
- **问题**: `tbody td` 里 `border-bottom: 1px solid var(--border-light);` 连写两遍。

### 21. CSS 无用类
- **文件**: `static/style.css:168`
- **问题**: `.table-fixed` 类定义了但从未使用。

### 22. #sharedConfirm z-index 过高
- **文件**: `static/style.css:345`
- **问题**: `z-index: 99999 !important;` 可能覆盖 toast 和详情面板。建议用结构化的 z-index 层级。

### 23. /account/{acc_id}/records 不校验账户是否存在
- **文件**: `main.py:289`
- **问题**: 传入不存在的账户ID，服务端不检查就渲染页面，前端拿到404后无提示。
- **修法**: 路由加 `db: Session = Depends(get_db)`，检查账户存在性。

### 24. /api/recurring 路由命名不一致
- **文件**: `main.py:831`
- **问题**: 其他资源用复数名词（`/api/expenses`），这个用单数。
- **修法**: 改为 `/api/recurring-expenses`（注意向后兼容）。

### 25. 回测表单无前端校验
- **文件**: `templates/backtest.html`
- **问题**: 起始日期 > 结束日期时不拦截，提交后 yfinance 可能报奇怪的错误。
- **修法**: `collectParams()` 或 `runBacktest()` 中加日期校验。

---

## ✅ 已完成（preload-rewrite）

- [x] 定投计划 CRUD + 自动执行
- [x] 自动补投历史
- [x] 标的信息自动获取（A股/美股/加密货币）
- [x] 多源行情数据（东方财富/新浪/CoinGecko/Yahoo Finance）
- [x] 持仓组合页3个版本（v1/v2/v3）
- [x] MarketPrice + TradingCalendar + PerformanceSnapshot 模型
- [x] 备份导入/导出（AES-256-GCM 加密，.enc 格式）
- [x] 文件选择器 accept 改为 .enc，提示文字改为 ENC（加密）
- [x] 删除废弃的 /api/backup/import 桩路由
- [x] database.py 迁移补上 dca_plans.start_date 列
- [x] 饼图按资产类型分组 + 收益折线对比
