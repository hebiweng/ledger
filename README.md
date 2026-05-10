# 和纸账簿 · Washi Ledger

本地个人财务管理系统，基于 FastAPI + SQLite，浏览器访问。

## 功能模块

**余额总览** — 账户余额月度管理，按账户类型分组小计，支持多币种汇率换算

**消费记录** — 日常支出记录、周期性支出自动生成、分类饼图

**投资记录** — 股票/ETF/基金/加密货币持仓管理，出入金转账，行情刷新，持仓组合分析

**回测引擎** — 可配置的定投+回撤加仓策略回测，支持 ATH 重置、快速上涨模式、月度/年度报表、PDF 导出

**数据管理** — AES-256 加密备份导出/导入，全量 9 张表

## 技术栈

- **后端**：Python 3.13 + FastAPI + SQLAlchemy + SQLite
- **前端**：Vanilla JS + Chart.js + Jinja2
- **数据源**：Yahoo Finance (yfinance)、东方财富 push2 API、CoinGecko

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py

# 浏览器打开
open http://127.0.0.1:8000
```

## 项目结构

```
├── main.py            # FastAPI 入口 + 全部 API
├── backtest.py        # 回测引擎
├── models.py          # 数据模型（9张表）
├── schemas.py         # Pydantic 请求校验
├── database.py        # 数据库连接
├── exchange_rate.py   # 汇率刷新
├── recurring.py       # 周期支出生成
├── static/style.css   # 样式
├── templates/         # Jinja2 页面模板
├── cache/             # 历史价格缓存（CSV）
└── output/            # PDF/HTML 报告生成
```

## 数据存储

所有数据存储在本地 `ledger.db`（SQLite），不上传云端。可通过加密备份功能导出 `.enc` 文件。

## License

MIT
