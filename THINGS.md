# Ledger v3 — 投资增强开发计划

> 目标：将 Ledger 从「手工记账工具」升级为「个人投资管理平台」
>
> 参考项目：Ghostfolio (TS/Prisma)、Portfolio Performance (Java)、Rotki (Python)、Actual Budget (TS/SQLite)
>
> 文档版本：2026-05-13 · 待评审

---

## 目录

1. [数据库重构](#1-数据库重构)
2. [数据来源设计与数据刷新架构](#2-数据来源设计与数据刷新架构)
3. [专业收益指标](#3-专业收益指标)
4. [资产配置可视化](#4-资产配置可视化)
5. [DCA 定投方案增强](#5-dca-定投方案增强)
6. [分红追踪模块](#6-分红追踪模块)
7. [实施分期计划](#7-实施分期计划)

---

## 1. 数据库重构

### 1.1 现状诊断

```
现有 12 张表：

  ✅ 保留不动 (7):
     accounts, monthly_balances, income_records, expense_records,
     expense_categories, recurring_expenses, trading_calendar

  ⚠️ 微调 (2):
     exchange_rates (加 date 维度), dca_plans (引 asset_profiles)

  🔴 必须拆分 (1):
     investment_records → trades + asset_profiles + dividends + cash_transactions

  🔴 必须替代 (1):
     performance_snapshots → portfolio_snapshots + portfolio_benchmarks

  🆕 必须新增 (2):
     asset_profiles, refresh_logs
```

### 1.2 核心问题：investment_records 是「神表」

```
investment_records 当前承载 5 种不同概念：

  type = 'buy'       → 买入交易，影响持仓数量 + 成本基础
  type = 'sell'      → 卖出交易，影响持仓数量 + 成本基础 + 盈亏
  type = 'dividend'  → 分红收入，不影响持仓，影响现金
  type = 'deposit'   → 入金，与标的无关，只是资金流动
  type = 'withdraw'  → 出金，与标的无关，只是资金流动
```

为什么必须拆？

- `buy/sell` 和 `deposit/withdraw` 是完全不同的查询维度：前者按 ticker 查，后者按 account 查
- `dividend` 不需要 quantity 和 price 字段，当前表中这些字段对分红行无意义（都是 NULL）
- 资产元信息（asset_type, currency, platform）在每笔交易中重复存储
- 后续做 FIFO 成本计算、分红率分析、资金流水时，SQL 会越来越复杂（WHERE type IN (...) + CASE WHEN）
- Ghostfolio 对此的教训：最早也是一张 transactions 表，后来拆成 Order + Dividend + Account，每次拆分都让代码更清晰

### 1.3 目标表结构

```
                                    ┌──────────────────────────────────┐
                                    │        asset_profiles ★NEW       │
                                    │──────────────────────────────────│
                                    │ ticker         TEXT PRIMARY KEY  │
                                    │ name            TEXT              │
                                    │ asset_type      TEXT              │  stock/etf/fund/crypto/bond/...
                                    │ asset_class     TEXT              │  equity/fixed_income/real_estate/...
                                    │ sector          TEXT              │  technology/finance/healthcare/...
                                    │ region          TEXT              │  US/CN/HK/Global/...
                                    │ currency        TEXT DEFAULT 'USD'│
                                    │ data_source     TEXT              │  eastmoney/yfinance/coingecko
                                    │ is_active       INT  DEFAULT 1    │
                                    │ created_at      TEXT              │
                                    │ updated_at      TEXT              │
                                    └──────────────────────────────────┘
                                              │
              ┌───────────────────────────────┼───────────────────────────────┐
              │                               │                               │
              ▼                               ▼                               ▼
┌──────────────────────────┐  ┌──────────────────────────┐  ┌──────────────────────────┐
│      trades ★SPLIT       │  │     dividends ★SPLIT      │  │  cash_transactions ★SPLIT │
│──────────────────────────│  │──────────────────────────│  │──────────────────────────│
│ id           INT PK      │  │ id           INT PK      │  │ id           INT PK      │
│ date         TEXT NN     │  │ date         TEXT NN     │  │ date         TEXT NN     │
│ type         TEXT NN     │  │ ticker       TEXT NN ────│─►│ type         TEXT NN     │
│   'buy' / 'sell'         │  │   FK→asset_profiles      │  │   'deposit' / 'withdraw' │
│ ticker       TEXT NN ────│─►│ amount_per_share REAL    │  │   / 'transfer'           │
│   FK→asset_profiles      │  │ total_amount REAL NN     │  │ from_account  INT ───────│──►
│ quantity     REAL        │  │ currency     TEXT         │  │   FK→accounts            │
│ price        REAL        │  │ account_id   INT ────────│─►│ to_account    INT ───────│──►
│ fees         REAL DEF 0  │  │   FK→accounts            │  │   FK→accounts            │
│ total_amount REAL NN     │  │ notes        TEXT         │  │ amount        REAL NN     │
│ currency     TEXT         │  │ created_at   TEXT         │  │ currency      TEXT        │
│ platform     TEXT         │  │ updated_at   TEXT         │  │ notes         TEXT        │
│ account_id   INT ────────│─►│                            │  │ created_at    TEXT        │
│   FK→accounts            │  │ 索引: (ticker, date)       │  │ updated_at    TEXT        │
│ notes        TEXT         │  │                            │  │                            │
│ created_at   TEXT         │  └──────────────────────────┘  │ 索引: (from_account, date) │
│ updated_at   TEXT         │                                 │       (to_account, date)   │
│                            │                                 │                            │
│ 索引: (ticker, date)      │                                 └──────────────────────────┘
│       (account_id, date)  │
│       (type)              │
└──────────────────────────┘
```

### 1.4 新增辅助表

```sql
-- 数据刷新日志 ★NEW
CREATE TABLE refresh_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,           -- 'market_price' / 'exchange_rate' / 'trading_calendar' / 'dividend'
    status          TEXT NOT NULL DEFAULT 'running',  -- 'running' / 'done' / 'failed'
    records_added   INTEGER DEFAULT 0,
    records_updated INTEGER DEFAULT 0,
    error_message   TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

-- 每日组合净值快照 ★替代 performance_snapshots
CREATE TABLE portfolio_snapshots (
    date            TEXT PRIMARY KEY,        -- YYYY-MM-DD
    unit_price      REAL,                    -- 组合单位净值 (初始=1)
    total_value_cny REAL,                    -- 总市值 (人民币)
    total_cost_cny  REAL,                    -- 总成本 (人民币)
    daily_change_pct REAL,                   -- 当日涨跌 %
    cash_flow_cny   REAL DEFAULT 0,          -- 当日净现金流 (入金-出金)
    holdings_count  INTEGER,
    created_at      TEXT
);

-- 基准收益对比 ★NEW (替代 performance_snapshots 里硬编码的 QQQ/SPY)
CREATE TABLE portfolio_benchmarks (
    date        TEXT NOT NULL,
    ticker      TEXT NOT NULL,              -- QQQ / SPY / CSI300 / ...
    close_price REAL NOT NULL,
    return_pct  REAL,                       -- 从基期起的累计收益率 %
    PRIMARY KEY (date, ticker)
);

-- 标签系统 ★NEW (可选，来自 Ghostfolio/Actual Budget/Firefly III)
CREATE TABLE tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE trade_tags (
    trade_id INTEGER REFERENCES trades(id) ON DELETE CASCADE,
    tag_id   INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (trade_id, tag_id)
);
```

### 1.5 微调现有表

```sql
-- exchange_rates: 加 date 维度，支持历史汇率
-- 之前: (from_currency, to_currency) 唯一
-- 之后: (date, from_currency, to_currency) 唯一
ALTER TABLE exchange_rates ADD COLUMN date TEXT DEFAULT (date('now'));
-- 注意：需要处理已有数据的 date 值

-- dca_plans: asset_name 改为引用 asset_profiles
-- 这个改在代码层面做 = 读取 dca_plans 时 JOIN asset_profiles 获取名称
-- 不需要改表结构，但 dca_plans.asset_name 的值应与 asset_profiles.ticker 保持一致

-- market_prices: 建议扩展字段
-- 之前: (ticker, date, close_price, change_pct, name, source, updated_at)
-- 建议加: open, high, low, volume（非必须，但 K线图需要）
-- 建议建一个联合视图或内存缓存，避免高频查询
```

### 1.6 数据迁移策略

```
Phase 1: 新建 + 并行运行
  1. 创建 asset_profiles 表
  2. 从 investment_records 提取唯一条目灌入 asset_profiles:
     INSERT INTO asset_profiles (ticker, name, asset_type, currency)
     SELECT DISTINCT asset_name, asset_name, asset_type, currency
     FROM investment_records
     WHERE asset_name IS NOT NULL
  3. 创建 trades 表，迁移 buy/sell 记录
  4. 创建 dividends 表
  5. 创建 cash_transactions 表，迁移 deposit/withdraw 记录
  6. 创建 refresh_logs、portfolio_snapshots、portfolio_benchmarks
  7. 保留 investment_records 表不动，前端 API 同时查新表（新表优先，旧表兜底）

Phase 2: 切换 + 废弃
  1. 前端全部切换到新表 API
  2. 删除 investment_records 表（或重命名为 _investment_records_old）
  3. 删除 performance_snapshots 表
```

---

## 2. 数据来源设计与数据刷新架构

### 2.1 数据源全景

#### A股 / 港股 / 基金

| 数据源 | 获取途径 | 数据类型 | 费用 | 可靠性 |
|--------|----------|----------|------|--------|
| **东方财富** | `akshare` 封装（推荐）/ 直接 HTTP API | A股日线、基金净值、可转债、行业板块、分红送配 | 免费 | ⭐⭐⭐⭐⭐ 最稳定 |
| **新浪财经** | `akshare` 封装 / `hq.sinajs.cn` | 实时行情（延迟15分钟）、交易日历 | 免费 | ⭐⭐⭐⭐ |
| **天天基金** | `akshare.fund_open_fund_info_em()` | 基金净值、基金持仓、基金经理 | 免费 | ⭐⭐⭐⭐ |
| **通达信 (pytdx)** | `pytdx` 库直连券商行情站 (TCP) | 日线/5分钟线、板块分类、财务数据 | 免费 | ⭐⭐⭐⭐ 不需要 token |
| **巨潮资讯** | `akshare.stock_dividents_cninfo()` | A股分红送配公告 | 免费 | ⭐⭐⭐ |

#### 美股 / 全球

| 数据源 | 获取途径 | 数据类型 | 费用 | 可靠性 |
|--------|----------|----------|------|--------|
| **Yahoo Finance** | `yfinance` (你已在用) | 美股日线、ETF、基本面、分红 | 免费 | ⭐⭐⭐ 偶有反爬 |
| **Polygon.io** | `polygon` Python 库 | 美股实时、历史、分红、拆股 | 免费层 5 calls/min | ⭐⭐⭐⭐⭐ |

#### 加密货币

| 数据源 | 获取途径 | 数据类型 | 费用 | 可靠性 |
|--------|----------|----------|------|--------|
| **CoinGecko** | `pycoingecko` (你已在用) | 加密货币行情 | 免费 | ⭐⭐⭐⭐ |

### 2.2 数据源抽象层设计

```python
# data_sources/
#   ├── __init__.py          # PriceSource 抽象基类 + 注册表
#   ├── eastmoney.py         # 东方财富 (A股/基金/板块/分红)
#   ├── yfinance_source.py   # Yahoo Finance (美股/ETF)
#   ├── coingecko_source.py  # CoinGecko (加密货币)
#   └── currency_source.py   # Frankfurter (汇率)

class PriceSource(ABC):
    """行情数据源抽象基类 — 学到 vnpy datafeed 插件化思路"""

    name: str
    supports: list[str]  # ['stock_cn', 'stock_us', 'fund_cn', 'crypto', 'fx']

    @abstractmethod
    def fetch_history(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """拉取历史日线 (date, open, high, low, close, volume)"""

    @abstractmethod
    def fetch_realtime(self, ticker: str) -> dict | None:
        """拉取实时快照 {price, change_pct, name, currency}"""

    def fetch_dividends(self, ticker: str, start: str, end: str) -> list[dict]:
        """拉取分红记录（可选实现）"""
        return []

    def resolve_ticker(self, code: str) -> dict:
        """根据代码推断资产类型、币种、名称（可选实现）"""
        return {"type": "stock", "currency": "USD", "name": None}


# 注册表 — 按资产类型自动路由到对应数据源
SOURCE_REGISTRY = {
    "stock_cn":  [EastMoneySource(), SinaSource()],
    "etf_cn":    [EastMoneySource()],
    "fund_cn":   [EastMoneySource()],    # 场外基金用天天基金
    "stock_us":  [YFinanceSource(), EastMoneyUSSource()],
    "etf_us":    [YFinanceSource()],
    "stock_hk":  [EastMoneySource()],
    "crypto":    [CoinGeckoSource()],
}
```

### 2.3 数据刷新架构

```
                         ┌──────────────────────┐
                         │   data_refresher.py   │
                         │──────────────────────│
                         │ refresh_market_prices │
                         │ refresh_exchange_rates│
                         │ refresh_trading_cal   │
                         │ refresh_dividends     │
                         │ refresh_benchmarks    │
                         └──────┬───────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
    ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
    │ APScheduler │   │  手动触发    │   │  启动时触发  │
    │(每天 8:30)  │   │ POST /api/   │   │ @lifespan    │
    │             │   │  refresh     │   │  异步后台    │
    └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
           │                 │                 │
           └────────┬────────┴────────┬────────┘
                    │                 │
                    ▼                 ▼
           ┌─────────────┐   ┌─────────────┐
           │ 拉取增量数据  │   │ 写入缓存表    │
           │ (增量: 只拉  │──►│ market_prices│
           │  缺失的日期)  │   │ exchange_rates│
           └─────────────┘   │ portfolio_   │
                             │ benchmarks   │
                             └──────┬──────┘
                                    │
                                    ▼
                           ┌─────────────────┐
                           │ 写入 refresh_logs│
                           │ status='done'   │
                           └────────┬────────┘
                                    │
                                    ▼
                           ┌─────────────────┐
                           │ GET /api/refresh │
                           │ -status          │
                           │ 前端轮询检查       │
                           │ (30秒 interval)   │
                           └────────┬────────┘
                                    │
                           有新的刷新记录?
                            │ is_new = (latest.
                            │ finished_at > 上次
                            │ 看到的时间)
                            │
                    ┌───────┴───────┐
                    │ YES           │ NO → 等待下一轮
                    ▼               │
            前端自动刷新数据
            (重拉 /api/portfolio
             /api/stats 等)
```

#### 定时调度配置

```python
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()

# 交易日 8:30 — A股开盘前刷新行情
scheduler.add_job(refresh_market_prices, 'cron', day_of_week='mon-fri', hour=8, minute=30)

# 每 6 小时 — 汇率刷新
scheduler.add_job(refresh_exchange_rates, 'interval', hours=6)

# 每天凌晨 2:00 — 交易日历同步
scheduler.add_job(refresh_trading_calendar, 'cron', hour=2, minute=0)

# 每周一 — 分红数据同步
scheduler.add_job(refresh_dividends, 'cron', day_of_week='mon', hour=6, minute=0)
```

#### 增量更新逻辑

```python
def incremental_update(db: Session, tickers: list[str], source: str):
    """
    只拉取缺失日期的数据，避免全量重复请求。

    步骤：
    1. 查询 market_prices 中每个 ticker 的最新日期
    2. 对每个 ticker，只请求 (latest_date + 1day) ~ today 的数据
    3. 新 ticker（从未拉过）请求最近 5 年数据
    4. 批量 INSERT OR IGNORE 写入
    """
    today = date.today().isoformat()
    for ticker in tickers:
        latest = db.query(MarketPrice.date).filter(
            MarketPrice.ticker == ticker
        ).order_by(MarketPrice.date.desc()).first()

        start = latest[0] if latest else (date.today() - timedelta(days=365*5)).isoformat()
        if start >= today:
            continue  # 数据已是最新

        df = fetch_price_history(ticker, start, today, source)
        upsert_market_prices(db, df, source)
```

### 2.4 前端通知机制

**第一期用轮询（最简单），第二期可上 SSE。**

```javascript
// 前端刷新状态轮询
let lastRefreshAt = null;

async function checkRefreshStatus() {
    const res = await fetch('/api/refresh-status');
    const data = await res.json();
    if (data.last_finished_at && data.last_finished_at !== lastRefreshAt) {
        lastRefreshAt = data.last_finished_at;
        // 有新数据，自动刷新页面数据
        loadPortfolio(false);
        loadCharts();
    }
}

// 每 30 秒检查一次
setInterval(checkRefreshStatus, 30000);
```

```python
# 后端 refresh-status 端点
@app.get("/api/refresh-status")
def api_refresh_status(db: Session = Depends(get_db)):
    latest = db.query(RefreshLog).order_by(
        RefreshLog.finished_at.desc()
    ).first()
    return {
        "last_finished_at": latest.finished_at if latest else None,
        "last_source": latest.source if latest else None,
        "last_status": latest.status if latest else None,
        "in_progress": db.query(RefreshLog).filter(
            RefreshLog.status == "running"
        ).count() > 0,
    }
```

---

## 3. 专业收益指标

### 3.1 核心理念：把组合当成「基金」来估值

```
Ghostfolio 的核心思路:
  每天计算一次 unit price = 组合总市值 / 基金总份额
  所有现金流（入金/出金）视为「份额申购/赎回」

  初始: unit_price = 1.0, total_shares = 初始投入金额
  入金: total_shares += 入金金额 / unit_price (按当日净值申购)
  出金: total_shares -= 出金金额 / unit_price (按当日净值赎回)
  每日: unit_price = 组合总市值 / total_shares

  TWR = unit_price_today / unit_price_start - 1
  （完全剔除了现金流时间差异的影响）
```

### 3.2 实现：`portfolio_metrics.py`

```python
"""
组合收益计算器 — 参考 Ghostfolio portfolio-calculator.ts

计算流程：
  1. 加载所有交易记录 (trades + dividends + cash_transactions)
  2. 确定时间范围 (第一笔交易的日期 → 今天)
  3. 加载每日行情 (从 market_prices 表)
  4. 按天迭代，计算每日 unit price 和 total_shares
  5. 从 unit price 时间序列导出所有指标
"""

def compute_unit_prices(
    db: Session,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """
    计算每日 unit price 和 shares 时间序列。

    返回 DataFrame:
      date, total_value_cny, cash_flow_cny, unit_price, total_shares, nav_cny
    """
    # 1. 加载交易
    trades = db.query(Trade).order_by(Trade.date).all()
    cash_flows = db.query(CashTransaction).order_by(CashTransaction.date).all()
    dividends = db.query(Dividend).order_by(Dividend.date).all()

    # 2. 确定日期范围
    all_dates = set()
    for t in trades:
        all_dates.add(t.date)
    for c in cash_flows:
        all_dates.add(c.date)
    for d in dividends:
        all_dates.add(d.date)

    if not all_dates:
        return pd.DataFrame()

    first_date = min(all_dates) if not start_date else start_date
    last_date = (date.today().isoformat() if not end_date else end_date)

    # 3. 加载行情
    tickers = list(set(t.asset_profile_ticker for t in trades))
    price_data = load_price_matrix(db, tickers, first_date, last_date)

    # 4. 逐日迭代
    date_range = pd.date_range(first_date, last_date, freq='D')
    rows = []
    shares = 0.0
    unit_price = 1.0

    # 按日期组织现金流
    cash_flow_map = defaultdict(float)
    for c in cash_flows:
        amount_cny = convert_to_cny(c.amount, c.currency, db)
        if c.type == 'deposit':
            cash_flow_map[c.date] += amount_cny
        elif c.type == 'withdraw':
            cash_flow_map[c.date] -= amount_cny

    # 按日期组织分红
    dividend_map = defaultdict(float)
    for d in dividends:
        amount_cny = convert_to_cny(d.total_amount, d.currency, db)
        dividend_map[d.date] += amount_cny

    # 持仓跟踪
    holdings = {}  # ticker → quantity

    for dt in date_range:
        date_str = dt.strftime("%Y-%m-%d")

        # 应用当日交易
        day_trades = [t for t in trades if t.date == date_str]
        for t in day_trades:
            ticker = t.asset_profile_ticker
            if t.type == 'buy':
                holdings[ticker] = holdings.get(ticker, 0) + (t.quantity or 0)
            elif t.type == 'sell':
                holdings[ticker] = holdings.get(ticker, 0) - (t.quantity or 0)

        # 计算当日总市值
        total_value = 0.0
        for ticker, qty in holdings.items():
            if qty <= 0:
                continue
            px = get_price(price_data, ticker, date_str)
            if px is None:
                # 查找最近的非空价格
                px = get_last_known_price(price_data, ticker, date_str)
            if px is not None:
                # 换算 CNY
                currency = get_asset_currency(db, ticker)
                if currency == 'CNY':
                    total_value += qty * px
                else:
                    conv = convert_to_cny(qty * px, currency, db)
                    total_value += conv['value'] if conv['valid'] else 0

        # 当日现金流（入金 + 分红）
        day_cash_flow = cash_flow_map.get(date_str, 0) + dividend_map.get(date_str, 0)

        # 更新份额
        if unit_price > 0:
            if day_cash_flow > 0:
                shares += day_cash_flow / unit_price  # 申购
            elif day_cash_flow < 0:
                shares += day_cash_flow / unit_price  # 赎回 (day_cash_flow 为负)

        # 计算 unit price
        if shares > 0:
            unit_price = total_value / shares
        else:
            unit_price = 1.0

        rows.append({
            "date": date_str,
            "total_value_cny": round(total_value, 2),
            "unit_price": round(unit_price, 6),
            "total_shares": round(shares, 2),
            "cash_flow_cny": round(day_cash_flow, 2),
            "nav_cny": round(total_value, 2),
        })

    return pd.DataFrame(rows)
```

### 3.3 指标计算

```python
def compute_twr(df: pd.DataFrame) -> float:
    """
    Time-Weighted Return — 时间加权收益率

    TWR = unit_price_today / unit_price_base - 1

    即在任意两个时点之间，TWR = 期末净值 / 期初净值 - 1
    这已经剔除了中间所有现金流的影响（因为现金流改变了份额，不改变净值）。
    """
    if df.empty:
        return 0.0
    return df["unit_price"].iloc[-1] / df["unit_price"].iloc[0] - 1


def compute_irr(df: pd.DataFrame) -> float:
    """
    Internal Rate of Return — 内部收益率 / 资金加权收益率

    解方程: Σ (cash_flow_i / (1 + r)^((t_i - t_0) / 365)) = 0

    用 numpy_financial 的 irr 函数，需要月度或日度现金流序列。
    IRR 考虑了入金时点——同样赚 10%，早期大量入金 vs 后期入金，IRR 不同。
    """
    if df.empty:
        return 0.0

    # 把现金流转为有时间权重的序列
    cash_flows = [-(df["nav_cny"].iloc[0])]  # 初始投入（负=流出）
    for _, row in df.iterrows():
        if row["cash_flow_cny"] != 0:
            cash_flows.append(-row["cash_flow_cny"])  # 入金=负(从你口袋出去)

    # 期末市值（正=如果你今天全卖掉拿回来的钱）
    cash_flows.append(df["nav_cny"].iloc[-1])

    # 年化 — 简化版用 numpy_financial
    try:
        from numpy_financial import irr as np_irr
        daily_irr = np_irr(cash_flows)
        annual_irr = (1 + daily_irr) ** 365 - 1
        return annual_irr
    except Exception:
        return 0.0


def compute_max_drawdown(df: pd.DataFrame) -> dict:
    """
    最大回撤

    遍历 unit_price 序列，追踪历史最高点。
    每次创新高 → 重置回撤。
    每次下跌 → 记录当前回撤 = (high - current) / high。
    返回最大回撤及其起止日期。
    """
    if df.empty:
        return {"mdd": 0, "peak_date": "", "trough_date": "", "recovery_date": ""}

    peak = df["unit_price"].iloc[0]
    mdd = 0.0
    peak_date = trough_date = recovery_date = ""

    for i, row in df.iterrows():
        up = row["unit_price"]
        if up > peak:
            peak = up
        drawdown = (peak - up) / peak
        if drawdown > mdd:
            mdd = drawdown
            trough_date = row["date"]
            peak_date = df[df["unit_price"] == peak]["date"].iloc[0]

    return {
        "mdd": round(mdd * 100, 2),        # 百分比
        "peak_date": peak_date,
        "trough_date": trough_date,
    }


def compute_rolling_returns(df: pd.DataFrame) -> dict:
    """
    滚动收益率

    从 unit_price 序列计算:
      YTD: 今年第一天到现在的收益率
      1月: 最近 21 个交易日
      3月: 最近 63 个交易日
      6月: 最近 126 个交易日
      1年: 最近 252 个交易日
      总: 全部
    """
    if df.empty:
        return {}

    up = df["unit_price"].values
    n = len(up)

    def rolling_return(window: int) -> float | None:
        if n <= window:
            return None
        return round((up[-1] / up[-(window + 1)] - 1) * 100, 2)

    # YTD
    ytd = None
    today = date.today()
    year_start = date(today.year, 1, 1).isoformat()
    ytd_rows = df[df["date"] >= year_start]
    if not ytd_rows.empty:
        ytd = round((up[-1] / ytd_rows["unit_price"].iloc[0] - 1) * 100, 2)

    return {
        "ytd": ytd,
        "1m": rolling_return(21),
        "3m": rolling_return(63),
        "6m": rolling_return(126),
        "1y": rolling_return(252),
        "3y": rolling_return(756),
        "total": round((up[-1] / up[0] - 1) * 100, 2),
    }
```

### 3.4 API 端点设计

```python
@app.get("/api/portfolio/metrics")
def api_portfolio_metrics(db: Session = Depends(get_db)):
    """
    返回组合全部专业指标。

    Response:
    {
      "twr_pct": 15.32,           # 时间加权收益率
      "irr_pct": 18.67,           # 内部收益率（年化）
      "mdd_pct": -12.5,           # 最大回撤
      "mdd_details": {...},       # 回撤详情
      "rolling": {                # 滚动收益
        "ytd": 5.2,
        "1m": 1.3, "3m": 4.5, "6m": 8.2,
        "1y": 15.3, "3y": 45.6, "total": 78.9
      },
      "benchmarks": {             # 基准对比
        "QQQ": { "twr_pct": 22.1, "mdd_pct": -15.3 },
        "SPY": { "twr_pct": 12.5, "mdd_pct": -10.1 },
        "CSI300": { "twr_pct": 3.2, "mdd_pct": -25.0 }
      },
      "snapshots": [...]          # 每日净值数据 (供图表)
    }
    """
    df = compute_unit_prices(db)
    twr = compute_twr(df)
    irr = compute_irr(df)
    mdd = compute_max_drawdown(df)
    rolling = compute_rolling_returns(df)
    benchmarks = compute_benchmark_returns(db, df["date"].iloc[0], df["date"].iloc[-1])

    return {
        "twr_pct": round(twr * 100, 2),
        "irr_pct": round(irr * 100, 2),
        "mdd_pct": mdd["mdd"],
        "mdd_details": mdd,
        "rolling": rolling,
        "benchmarks": benchmarks,
        "snapshots": df.to_dict(orient="records"),
    }
```

### 3.5 前端展示（投资分析卡片）

```
┌─────────────────────────────────────────────────────────┐
│  投资分析                                                │
├──────────────┬──────────────┬──────────────┬────────────┤
│  TWR 累计收益  │  IRR 年化收益 │  最大回撤      │  今日涨跌    │
│  +78.9% 🟢   │  +18.7% 🟢   │  -12.5%      │  +1.2% 🟢   │
├──────────────┴──────────────┴──────────────┴────────────┤
│                                                         │
│  [净值曲线 + 基准对比 Chart.js 折线图]                      │
│  — 组合净值  — QQQ  — SPY  — CSI300                     │
│                                                         │
├──────────────┬──────────────┬──────────────┬────────────┤
│  滚动收益     │  1月    3月    6月    YTD    1年    总     │
│              │ +1.3%  +4.5%  +8.2% +5.2% +15.3% +78.9%  │
└──────────────┴──────────────┴──────────────┴────────────┘
```

---

## 4. 资产配置可视化

### 4.1 数据来源：asset_profiles 表

这是整个配置可视化的数据基础。关键在于：每行资产记录必须填写 `sector` 和 `region`。

```sql
-- asset_profiles 中的配置相关字段
asset_type   → 按类型聚合 (ETF / 个股 / 债券 / 加密货币 / 现金)
sector       → 按行业聚合 (科技 / 金融 / 医疗 / 消费 / 能源 / ...)
region       → 按地域聚合 (US / CN / HK / Global / ...)
currency     → 按币种聚合
```

#### 自动填充策略

```python
def auto_fill_asset_profile(ticker: str, asset_type: str, db: Session) -> dict:
    """
    新建资产时自动填充元信息。

    优先从 market_prices / akshare / yfinance 获取 name。
    sector 和 region 需要一个映射表（或从 akshare 板块获取）。
    """
    profile = {"name": ticker, "sector": None, "region": None}

    if asset_type in ("stock", "etf"):
        if ticker.isdigit() and len(ticker) == 6:
            # A股 — 从 akshare 获取行业
            try:
                info = ak.stock_individual_info_em(symbol=ticker)
                profile["name"] = info.loc["股票简称", "value"]
                # 行业用 akshare 板块分类 或 pytdx
            except Exception:
                pass
            profile["region"] = "CN"
        elif ticker.isalpha():
            # 美股 — 从 yfinance 获取
            try:
                tk = yf.Ticker(ticker)
                info = tk.info
                profile["name"] = info.get("longName") or info.get("shortName", ticker)
                profile["sector"] = info.get("sector")
                profile["region"] = "US"
            except Exception:
                pass

    return profile
```

#### 行业/地域映射表

```python
# 维护一个静态映射作为兜底
KNOWN_ETFS = {
    "QQQ":   {"name": "Invesco QQQ Trust",       "sector": "科技",     "region": "US",   "asset_class": "equity"},
    "SPY":   {"name": "SPDR S&P 500 ETF",        "sector": "综合",     "region": "US",   "asset_class": "equity"},
    "TLT":   {"name": "iShares 20+ Year Treasury","sector": "债券",     "region": "US",   "asset_class": "fixed_income"},
    "GLD":   {"name": "SPDR Gold Trust",          "sector": "大宗商品", "region": "Global","asset_class": "commodity"},
    "VNQ":   {"name": "Vanguard Real Estate ETF", "sector": "房地产",   "region": "US",   "asset_class": "real_estate"},
    "CSI300": {"name": "沪深300 ETF",             "sector": "综合",     "region": "CN",   "asset_class": "equity"},
    # ... 按需扩展
}
```

### 4.2 分配计算 API

```python
@app.get("/api/portfolio/allocation")
def api_portfolio_allocation(db: Session = Depends(get_db)):
    """
    返回持仓的多种维度聚合。

    Response:
    {
      "by_type":     [{"label": "ETF",    "value": 350000, "pct": 58.3}, ...],
      "by_region":   [{"label": "美股",    "value": 420000, "pct": 70.0}, ...],
      "by_sector":   [{"label": "科技",    "value": 280000, "pct": 46.7}, ...],
      "by_currency": [{"label": "USD",    "value": 480000, "pct": 80.0}, ...],
      "by_asset":    [{"label": "QQQ",    "value": 150000, "pct": 25.0}, ...],
      "total_cny":   600000,
      "drift_alerts": [...]  # 配置漂移提示
    }
    """
    # 1. 计算当前持仓（复用 /api/portfolio 的逻辑）
    portfolio = api_portfolio(refresh=False, db=db)
    holdings = portfolio["holdings"]

    # 2. 获取资产元信息
    tickers = [h["asset_name"] for h in holdings]
    profiles = db.query(AssetProfile).filter(
        AssetProfile.ticker.in_(tickers)
    ).all()
    profile_map = {p.ticker: p for p in profiles}

    # 3. 多维度聚合
    by_type = defaultdict(float)
    by_region = defaultdict(float)
    by_sector = defaultdict(float)
    by_currency = defaultdict(float)
    by_asset = []

    for h in holdings:
        current_value_cny = h.get("current_value") or 0
        if current_value_cny <= 0:
            continue

        p = profile_map.get(h["asset_name"])
        asset_type = p.asset_type if p else h["asset_type"]
        sector = p.sector if p and p.sector else "未分类"
        region = p.region if p and p.region else "其他"

        by_type[asset_type] += current_value_cny
        by_region[region] += current_value_cny
        by_sector[sector] += current_value_cny
        by_currency[h["currency"]] += current_value_cny
        by_asset.append({
            "label": h["asset_name"],
            "value": round(current_value_cny, 2),
            "display_name": h.get("display_name") or p.name if p else None,
        })

    total = sum(v for v in by_type.values())

    def fmt(cat_dict):
        return sorted(
            [{"label": k, "value": round(v, 2), "pct": round(v / total * 100, 1)}
             for k, v in cat_dict.items() if v > 0],
            key=lambda x: -x["value"]
        )

    return {
        "by_type": fmt(by_type),
        "by_region": fmt(by_region),
        "by_sector": fmt(by_sector),
        "by_currency": fmt(by_currency),
        "by_asset": sorted(by_asset, key=lambda x: -x["value"]),
        "total_cny": round(total, 2),
    }


def compute_drift_alerts(portfolio: dict, targets: dict, tolerance: float = 5.0) -> list[dict]:
    """
    配置漂移检测。

    targets = {"ETF": 60, "个股": 20, "债券": 10, "加密货币": 5, "现金": 5}  # 目标百分比
    tolerance = 5  # 偏离超过 ±5% 告警

    返回:
    [
      {"label": "ETF", "current_pct": 72.5, "target_pct": 60.0, "drift": 12.5, "action": "卖出部分 ETF 或增加其他配置"},
      {"label": "债券", "current_pct": 3.2, "target_pct": 10.0, "drift": -6.8, "action": "增持债券"},
    ]
    """
    current = {item["label"]: item["pct"] for item in portfolio.get("by_type", [])}
    alerts = []
    for label, target_pct in targets.items():
        current_pct = current.get(label, 0)
        drift = current_pct - target_pct
        if abs(drift) > tolerance:
            if drift > 0:
                action = f"建议卖出部分 {label} 或增加其他配置"
            else:
                action = f"建议增持 {label}"
            alerts.append({
                "label": label,
                "current_pct": round(current_pct, 1),
                "target_pct": target_pct,
                "drift": round(drift, 1),
                "action": action,
            })
    return alerts
```

### 4.3 前端展示

```
┌─────────────────────────────────────────────────────────┐
│  资产配置                                                │
├────────────────────────────┬────────────────────────────┤
│  按资产类型 (饼图)          │  按地域分布 (饼图)          │
│  ┌──────────────────────┐ │ ┌──────────────────────┐   │
│  │  ETF      58%  🟦    │ │  │  美股     70%  🟦    │   │
│  │  个股     22%  🟩    │ │  │  A股      18%  🟥    │   │
│  │  债券     10%  🟨    │ │  │  港股      8%  🟩    │   │
│  │  加密货币  5%  🟪    │ │  │  其他      4%  🟨    │   │
│  │  现金      5%  🟧    │ │  └──────────────────────┘   │
│  └──────────────────────┘ │                            │
├────────────────────────────┼────────────────────────────┤
│  按行业分布 (饼图)          │  按币种分布 (饼图)          │
│  ┌──────────────────────┐ │ ┌──────────────────────┐   │
│  │  科技     47%  🟦    │ │  │  USD      80%  🟦    │   │
│  │  金融     18%  🟩    │ │  │  CNY      15%  🟩    │   │
│  │  医疗     12%  🟨    │ │  │  HKD       5%  🟨    │   │
│  │  消费      8%  🟧    │ │  └──────────────────────┘   │
│  │  其他     15%  🟪    │ │                            │
│  └──────────────────────┘ │                            │
├───────────────────────────┴────────────────────────────┤
│  ⚠️ 配置漂移提示:                                       │
│  • ETF 当前 72.5%，目标 60%，偏离 +12.5% → 卖出部分 ETF │
│  • 债券 当前 3.2%，目标 10%，偏离 -6.8% → 增持债券      │
└─────────────────────────────────────────────────────────┘
```

### 4.4 图表实现 (Chart.js)

```javascript
// 多维度饼图切换
const allocationData = await fetch('/api/portfolio/allocation').then(r => r.json());

const chartCtx = document.getElementById('chartTypePie').getContext('2d');
let allocChart = null;

function showAllocation(dimension) {
    const data = allocationData[dimension]; // 'by_type' / 'by_region' / 'by_sector' / 'by_currency'
    if (allocChart) allocChart.destroy();

    allocChart = new Chart(chartCtx, {
        type: 'doughnut',
        data: {
            labels: data.map(d => d.label),
            datasets: [{
                data: data.map(d => d.value),
                backgroundColor: [
                    '#3b82f6', '#22c55e', '#eab308', '#a855f7', '#f97316',
                    '#ef4444', '#06b6d4', '#ec4899', '#84cc16', '#6366f1',
                ],
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: { position: 'right' },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `${ctx.label}: ¥${ctx.raw.toLocaleString()} (${data[ctx.dataIndex].pct}%)`
                    }
                }
            }
        }
    });
}
```

---

## 5. DCA 定投方案增强

### 5.1 条件触发规则

参考 Actual Budget 的 scheduled transactions 和 Portfolio Performance 的条件规则。

```python
# 新增: DCA 条件规则配置
class DcaConditionType(str, Enum):
    ALWAYS = "always"                # 无条件执行 (当前行为)
    BELOW_MA = "below_ma"           # 低于均线时加倍
    ABOVE_MA_SKIP = "above_ma_skip" # 高于均线时跳过
    BELOW_DRAWDOWN = "below_drawdown" # 跌破某价位时加倍
    CASH_THRESHOLD = "cash_threshold" # 资金来源余额不足则跳过
```

#### models.py 新增字段

```python
class DcaPlan(Base):
    __tablename__ = "dca_plans"

    # ... 现有字段保留 ...

    # ★新增字段
    condition_type = Column(String, default="always")  # 条件类型
    condition_params = Column(String, default="{}")     # JSON: {"ma_days": 60, "multiplier": 2}
    target_asset_pct = Column(Float, nullable=True)     # 目标配置比例 (用于再平衡联动)
    max_cash_ratio = Column(Float, default=0.9)         # 资金来源最多使用 90%
    total_executions = Column(Integer, default=0)       # 总执行次数
    total_amount_cny = Column(Float, default=0.0)       # 总投入金额 (人民币)
```

#### 执行逻辑增强

```python
def evaluate_dca_condition(plan: DcaPlan, db: Session) -> DcaDecision:
    """
    评估定投条件，返回决策。

    返回:
      DcaDecision(action='execute', amount=5000, reason='正常定投')
      DcaDecision(action='double', amount=10000, reason='低于 60 日均线')
      DcaDecision(action='skip', amount=0, reason='高于均线，跳过')
      DcaDecision(action='skip', amount=0, reason='资金来源余额不足')
    """

    # 1. 现金检查
    if plan.payment_account:
        balance = get_account_balance(plan.payment_account, db)
        required = plan.amount
        if balance < required * (1 - plan.max_cash_ratio):
            return DcaDecision("skip", 0, f"资金来源余额不足 (可用: ¥{balance:,.0f}, 需要: ¥{required:,.0f})")

    # 2. 均线条件
    if plan.condition_type in ("below_ma", "above_ma_skip"):
        params = json.loads(plan.condition_params or "{}")
        ma_days = params.get("ma_days", 60)
        multiplier = params.get("multiplier", 2)

        current_price = get_current_price(plan.asset_name, db)
        ma_price = get_moving_average(plan.asset_name, ma_days, db)

        if current_price and ma_price:
            if plan.condition_type == "below_ma" and current_price < ma_price:
                return DcaDecision("double", plan.amount * multiplier,
                                   f"当前价 ¥{current_price} < {ma_days}日均价 ¥{ma_price:.2f}，加倍买入")
            elif plan.condition_type == "above_ma_skip" and current_price > ma_price:
                return DcaDecision("skip", 0,
                                   f"当前价 ¥{current_price} > {ma_days}日均价 ¥{ma_price:.2f}，跳过")

    # 3. 无条件执行
    return DcaDecision("execute", plan.amount, "正常定投")
```

### 5.2 FIFO 卖出成本计算

```python
def compute_fifo_cost_basis(ticker: str, sell_quantity: float, db: Session) -> dict:
    """
    FIFO 卖出成本计算。

    按买入时间顺序，先进先出。
    返回: {
        "cost_basis": 15000.0,       # 卖出部分的成本
        "realized_gain": 5000.0,     # 实现盈亏
        "matched_lots": [            # 匹配的买入批次
            {"buy_date": "2024-01-15", "quantity": 50, "buy_price": 100, "cost": 5000},
            {"buy_date": "2024-03-20", "quantity": 50, "buy_price": 200, "cost": 10000},
        ]
    }
    """
    # 获取所有未匹配的买入记录，按日期升序
    buys = db.query(Trade).filter(
        Trade.ticker == ticker,
        Trade.type == "buy",
    ).order_by(Trade.date.asc()).all()

    remaining = sell_quantity
    matched_lots = []
    total_cost = 0.0

    for buy in buys:
        if remaining <= 0:
            break
        # 考虑之前已经卖出过的部分（需要追踪已匹配量）
        available = buy.quantity - get_matched_quantity(buy.id, db)
        if available <= 0:
            continue

        take = min(available, remaining)
        cost = take * buy.price
        total_cost += cost
        matched_lots.append({
            "buy_date": buy.date,
            "buy_id": buy.id,
            "quantity": take,
            "buy_price": buy.price,
            "cost": cost,
        })
        remaining -= take

    return {
        "cost_basis": round(total_cost, 2),
        "matched_lots": matched_lots,
        "unmatched_quantity": round(remaining, 4) if remaining > 0 else 0,
    }
```

### 5.3 增强 DCA API

```python
@app.post("/api/dca-plans/{plan_id}/execute")
def api_dca_execute_enhanced(plan_id: int, db: Session = Depends(get_db)):
    """
    执行定投 — 增强版。
    先评估条件，再决定执行方案。
    """
    plan = db.query(DcaPlan).filter(DcaPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "定投计划不存在")

    # 1. 评估条件
    decision = evaluate_dca_condition(plan, db)

    # 2. 根据决策执行
    if decision.action == "skip":
        return {"executed": False, "reason": decision.reason}

    amount = decision.amount
    # 3. 拉取当前价格
    price = get_current_price(plan.asset_name, db)
    if not price:
        raise HTTPException(400, f"无法获取 {plan.asset_name} 的价格")

    # 4. 创建交易记录
    quantity = amount / price
    trade = Trade(
        date=date.today().isoformat(),
        type="buy",
        ticker=plan.asset_name,
        quantity=quantity,
        price=price,
        fees=plan.fees or 0,
        total_amount=amount,
        currency=plan.currency,
        platform=plan.platform,
        account_id=plan.account_id,
        notes=f"DCA 定投 [{plan.asset_name}] {decision.reason}",
    )
    db.add(trade)

    # 5. 更新 DCA 统计
    plan.total_executions += 1
    plan.total_amount_cny += convert_to_cny(amount, plan.currency, db)["value"] or amount
    plan.next_date = compute_next_date(plan.frequency, plan.next_date)
    db.commit()

    return {
        "executed": True,
        "reason": decision.reason,
        "quantity": round(quantity, 4),
        "price": price,
        "amount": amount,
        "next_date": plan.next_date,
    }
```

---

## 6. 分红追踪模块

### 6.1 独立的分红表

```sql
-- dividends 表 (从 investment_records 中 type='dividend' 的记录迁移)
CREATE TABLE dividends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT NOT NULL,              -- 除息日 / 到账日
    ticker              TEXT NOT NULL REFERENCES asset_profiles(ticker),
    amount_per_share    REAL,                       -- 每股分红
    total_amount        REAL NOT NULL,              -- 总金额
    currency            TEXT DEFAULT 'USD',
    account_id          INTEGER REFERENCES accounts(id),  -- 到账账户
    dividend_type       TEXT DEFAULT 'cash',       -- 'cash' / 'stock' (股票分红/拆股)
    notes               TEXT,
    created_at          TEXT,
    updated_at          TEXT
);

CREATE INDEX idx_dividends_ticker_date ON dividends(ticker, date);
CREATE INDEX idx_dividends_account ON dividends(account_id, date);
```

### 6.2 分红数据自动获取

```python
def fetch_dividends_for_ticker(ticker: str, asset_type: str, db: Session) -> int:
    """
    拉取单个标的的历史分红记录。

    A股: akshare.stock_dividents_cninfo() 或 akshare.stock_fhps_em()
    美股: yfinance Ticker.dividends 或 polygon.io
    """
    new_count = 0

    if asset_type in ("stock", "etf"):
        # 判断市场
        profile = db.query(AssetProfile).filter(AssetProfile.ticker == ticker).first()
        region = profile.region if profile else None

        if region == "CN" or (ticker.isdigit() and len(ticker) == 6):
            # A股分红
            try:
                df = ak.stock_fhps_em(symbol=ticker)
                for _, row in df.iterrows():
                    ex_date = str(row.get("除权除息日", ""))
                    if not ex_date:
                        continue
                    # 检查是否已存在
                    existing = db.query(Dividend).filter(
                        Dividend.ticker == ticker,
                        Dividend.date == ex_date,
                    ).first()
                    if existing:
                        continue
                    db.add(Dividend(
                        date=ex_date,
                        ticker=ticker,
                        amount_per_share=float(row.get("派息比例", 0) or 0),
                        total_amount=0,  # 需要根据当时的持仓量计算
                        currency="CNY",
                        dividend_type="cash",
                        notes=f"A股分红: {row.get('方案说明', '')}",
                    ))
                    new_count += 1
            except Exception as e:
                print(f"  分红拉取失败 {ticker}: {e}")

        elif region == "US" or ticker.isalpha():
            # 美股分红 (yfinance)
            try:
                tk = yf.Ticker(ticker)
                div_df = tk.dividends
                if div_df is not None and not div_df.empty:
                    for dt, amount in div_df.items():
                        date_str = dt.strftime("%Y-%m-%d") if hasattr(dt, 'strftime') else str(dt)[:10]
                        existing = db.query(Dividend).filter(
                            Dividend.ticker == ticker,
                            Dividend.date == date_str,
                        ).first()
                        if existing:
                            continue
                        db.add(Dividend(
                            date=date_str,
                            ticker=ticker,
                            amount_per_share=float(amount),
                            total_amount=0,  # 需要根据持仓计算
                            currency="USD",
                            dividend_type="cash",
                            notes="美股分红 (yfinance)",
                        ))
                        new_count += 1
            except Exception as e:
                print(f"  分红拉取失败 {ticker}: {e}")

    db.commit()
    return new_count
```

### 6.3 分红分析 API

```python
@app.get("/api/dividends")
def api_dividends(
    ticker: str | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
):
    """
    分红记录查询。

    支持：
    - 按标的筛选: ?ticker=QQQ
    - 按年份筛选: ?year=2025
    - 全量返回: 无参数

    Response:
    {
      "records": [...],
      "summary": {
        "total_dividends_cny": 8500.00,
        "ytd_dividends_cny": 1200.00,
        "current_yield_pct": 1.85,
        "avg_annual_yield_pct": 2.10,
        "top_dividend_payers": [...]
      },
      "calendar": [...]  // 未来30天预期分红
    }
    """
    query = db.query(Dividend)
    if ticker:
        query = query.filter(Dividend.ticker == ticker)
    if year:
        query = query.filter(Dividend.date >= f"{year}-01-01", Dividend.date <= f"{year}-12-31")

    records = query.order_by(Dividend.date.desc()).all()

    # 汇总计算
    total_cny = sum(
        convert_to_cny(r.total_amount, r.currency, db)["value"] or 0
        for r in records
    )
    this_year = date.today().year
    ytd_cny = sum(
        convert_to_cny(r.total_amount, r.currency, db)["value"] or 0
        for r in records if r.date.startswith(str(this_year))
    )

    # 分红率计算
    portfolio = api_portfolio(refresh=False, db=db)
    current_value = portfolio["summary"]["total_value_cny"]
    current_yield = (ytd_cny / current_value * 100) if current_value > 0 else 0

    return {
        "records": [{
            "id": r.id,
            "date": r.date,
            "ticker": r.ticker,
            "total_amount": r.total_amount,
            "currency": r.currency,
            "amount_per_share": r.amount_per_share,
            "dividend_type": r.dividend_type,
        } for r in records],
        "summary": {
            "total_dividends_cny": round(total_cny, 2),
            "ytd_dividends_cny": round(ytd_cny, 2),
            "current_yield_pct": round(current_yield, 2),
        },
    }
```

### 6.4 前端分红卡片

```
┌─────────────────────────────────────────────────────────┐
│  分红收入                                                │
├──────────────┬──────────────┬──────────────┬────────────┤
│  累计分红      │  今年分红      │  当前分红率    │  月均分红    │
│  ¥8,500      │  ¥1,200      │  1.85%       │  ¥708       │
├──────────────┴──────────────┴──────────────┴────────────┤
│  年度分红趋势 (Chart.js 柱状图)                           │
│  ██                                                      │
│  ██ 2023: ¥2,100                                        │
│  ██ 2024: ¥3,800                                        │
│  ██ 2025: ¥1,200 (YTD)                                  │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  分红明细                                                │
│  日期        │ 标的     │ 金额      │ 币种  │ 类型        │
│  2025-03-15  │ QQQ     │ ¥892.00  │ USD   │ 现金分红    │
│  2025-01-10  │ SPY     │ ¥308.00  │ USD   │ 现金分红    │
└─────────────────────────────────────────────────────────┘
```

---

## 7. 实施分期计划

### Phase 1: 数据基础 (1-2 天)

```
□ 1.1 新建 asset_profiles 表 + 从 investment_records 提取初始数据
□ 1.2 新建 refresh_logs 表
□ 1.3 新建 portfolio_snapshots 表
□ 1.4 新建 portfolio_benchmarks 表
□ 1.5 exchange_rates 加 date 字段
□ 1.6 创建 DataRefresher 类 + APScheduler 定时任务
□ 1.7 实现增量更新逻辑 (incremental_update)
□ 1.8 引入 akshare 为数据源，封装 EastMoneySource
□ 1.9 实现 /api/refresh-status 端点
```

### Phase 2: 投资核心 (2-3 天)

```
□ 2.1 拆分 investment_records:
      - 创建 trades 表，迁移 buy/sell 记录
      - 创建 dividends 表，迁移 dividend 记录
      - 创建 cash_transactions 表，迁移 deposit/withdraw 记录
      - 保留旧表作为兜底，前端 API 改用新表
□ 2.2 实现 portfolio_metrics.py:
      - compute_unit_prices()
      - compute_twr(), compute_irr(), compute_max_drawdown()
      - compute_rolling_returns()
□ 2.3 实现 /api/portfolio/metrics 端点
□ 2.4 实现 /api/portfolio/allocation 端点
□ 2.5 实现 compute_drift_alerts()
□ 2.6 前端: 投资分析卡片 + 收益曲线 + 基准对比 Chart.js 图
```

### Phase 3: 配置可视化 + DCA 增强 (1-2 天)

```
□ 3.1 实现 asset_profiles 自动填充 (akshare/yfinance)
□ 3.2 前端: 四个分配维度饼图 + 切换交互
□ 3.3 前端: 目标配置设置面板 + 漂移提示
□ 3.4 DCA 条件规则 (evaluate_dca_condition)
□ 3.5 DCA FIFO 卖出成本计算 (compute_fifo_cost_basis)
□ 3.6 DCA 执行增强 (/api/dca-plans/{id}/execute 增强版)
```

### Phase 4: 分红模块 (1 天)

```
□ 4.1 dividends 表完善 + 数据获取
□ 4.2 /api/dividends 端点 (查询 + 汇总)
□ 4.3 前端: 分红收入卡片 + 年度趋势图 + 分红明细
□ 4.4 分红数据定时刷新 (每周一)
```

### Phase 5: 清理 + 文档 (0.5 天)

```
□ 5.1 废弃旧 investment_records 表 (重命名为 _old)
□ 5.2 废弃旧 performance_snapshots 表
□ 5.3 全链路测试: 创建交易 → 刷新行情 → 查看指标 → 分配图 → DCA → 分红
□ 5.4 更新 DESIGN.md / README.md / TODO.md
```

---

## 附录: 文件结构规划

```
ledger/
├── main.py                      # FastAPI 入口 (精简，路由分模块)
├── models.py                    # 数据模型 (本次大幅修改)
├── schemas.py                   # Pydantic 校验 (新增)
├── database.py                  # 数据库连接 + 迁移 (保持)
├── exchange_rate.py             # 汇率 (加 date 字段)
├── backtest.py                  # 回测引擎 (保持)
├── recurring.py                 # 定期支出 (保持)
│
├── data_sources/                # ★新建: 数据源抽象层
│   ├── __init__.py              # PriceSource 基类 + 注册表
│   ├── eastmoney.py             # 东方财富 (A股/基金/行业/分红)
│   ├── yfinance_source.py       # Yahoo Finance (美股/ETF/分红)
│   ├── coingecko_source.py      # CoinGecko (加密货币)
│   └── currency_source.py       # Frankfurter (汇率)
│
├── services/                    # ★新建: 业务逻辑层
│   ├── __init__.py
│   ├── data_refresher.py        # 定时 + 手动数据刷新
│   ├── portfolio_metrics.py     # TWR/IRR/MDD/滚动收益
│   ├── allocation.py            # 资产配置计算 + 漂移检测
│   ├── dca_executor.py          # DCA 条件评估 + 执行
│   └── dividend_tracker.py      # 分红拉取 + 分析
│
├── templates/
│   ├── base.html                # 基础布局 (保持)
│   ├── investment.html          # 投资主页 (大幅修改)
│   ├── investment_charts.html   # ★新增: 投资分析图表局部
│   ├── allocation.html          # ★新增: 资产配置局部
│   ├── dividends.html           # ★新增: 分红追踪局部
│   └── ... (其他页面保持)
│
└── static/
    ├── style.css
    └── js/
        ├── portfolio.js         # ★新增: 投资组合 JS
        ├── allocation.js        # ★新增: 资产配置 JS
        ├── metrics.js           # ★新增: 指标 JS
        └── dividends.js         # ★新增: 分红 JS
```

---

> 参考项目:
> - Ghostfolio: [Prisma schema](https://github.com/ghostfolio/ghostfolio/blob/main/prisma/schema.prisma), [portfolio-calculator.ts](https://github.com/ghostfolio/ghostfolio/blob/main/apps/api/src/services/portfolio/portfolio-calculator.ts)
> - Actual Budget: [transaction model](https://github.com/actualbudget/actual/blob/master/packages/loot-core/src/types/models/transaction.ts)
> - Maybe Finance: [db/migrate](https://github.com/maybe-finance/maybe/tree/main/db/migrate)
> - vnpy: [datafeed](https://github.com/vnpy/vnpy), [gateway](https://www.vnpy.com/docs/cn/gateway.html)
> - AKShare: [akshare docs](https://akshare.akfamily.xyz/)
