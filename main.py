"""Personal ledger web app — FastAPI entry point."""
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException, Form, UploadFile, File
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from database import engine, get_db, init_db
from models import (
    Base, PRESET_CATEGORIES,
    Account, MonthlyBalance, IncomeRecord,
    ExpenseRecord, ExpenseCategory, RecurringExpense,
    InvestmentRecord, ExchangeRate, DcaPlan, MarketPrice,
    PerformanceSnapshot, TradingCalendar,
)
from schemas import (
    AccountCreate, AccountUpdate,
    MonthlyBalanceSave,
    IncomeCreate, IncomeUpdate,
    ExpenseCreate, ExpenseUpdate,
    CategoryCreate,
    RecurringCreate, RecurringUpdate,
    InvestmentCreate, InvestmentUpdate,
)
from exchange_rate import get_rate, convert_to_cny, refresh_all_rates
from recurring import ensure_expenses_for_month


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def seed_categories(db: Session):
    existing = db.query(ExpenseCategory).count()
    if existing == 0:
        for i, name in enumerate(PRESET_CATEGORIES):
            db.add(ExpenseCategory(name=name, sort_order=i, is_preset=1))
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = next(get_db())
    try:
        seed_categories(db)
        refresh_all_rates(db)
    finally:
        db.close()
    yield


app = FastAPI(title="个人账本", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
jinja_env = Environment(loader=FileSystemLoader("templates"), autoescape=True)


# ── Page routes ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def page_index():
    tpl = jinja_env.get_template("index.html")
    return HTMLResponse(tpl.render(nav="index"))


@app.get("/expenses", response_class=HTMLResponse)
def page_expenses():
    tpl = jinja_env.get_template("expenses.html")
    return HTMLResponse(tpl.render(nav="expenses"))


@app.get("/investment2", response_class=HTMLResponse)
def page_investment2(db: Session = Depends(get_db)):
    tpl = jinja_env.get_template("investment2.html")
    return HTMLResponse(tpl.render(nav="investment2", preload="{}"))

@app.get("/investment3", response_class=HTMLResponse)
def page_investment3(db: Session = Depends(get_db)):
    # Reuse same preload data as main investment page
    import json as _json
    now = datetime.now()
    # Accounts
    type_order = {"bank": 0, "credit": 1, "ewallet": 2, "investment": 3, "topup": 4, "loan": 5, "cash": 6}
    accounts = db.query(Account).filter(Account.is_active == 1, Account.parent_id == None).all()
    accounts.sort(key=lambda a: (type_order.get(a.type, 99), a.sort_order))
    accounts_data = [{"id": a.id, "name": a.name, "type": a.type, "currency": a.currency, "is_active": a.is_active} for a in accounts]
    # Investment records
    inv_rows = db.query(InvestmentRecord).order_by(InvestmentRecord.date.desc()).all()
    investments_data = [{"id": r.id, "date": r.date, "type": r.type, "asset_name": r.asset_name, "asset_type": r.asset_type, "quantity": r.quantity, "price": r.price, "fees": r.fees, "total_amount": r.total_amount, "currency": r.currency, "platform": r.platform or "", "account_id": r.account_id, "notes": r.notes or ""} for r in inv_rows]
    # DCA
    dca_rows = db.query(DcaPlan).order_by(DcaPlan.is_active.desc(), DcaPlan.next_date).all()
    dca_data = [{"id": r.id, "asset_name": r.asset_name, "asset_type": r.asset_type, "amount": r.amount, "fees": r.fees or 0, "currency": r.currency, "frequency": r.frequency, "start_date": r.start_date, "next_date": r.next_date, "is_active": r.is_active} for r in dca_rows]
    # Add names from market_prices cache for DCA assets
    dca_names = {}
    for d in dca_data:
        mp = db.query(MarketPrice).filter(MarketPrice.ticker == d["asset_name"]).order_by(MarketPrice.date.desc()).first()
        if mp and mp.name and mp.name != d["asset_name"]:
            dca_names[d["asset_name"]] = mp.name
    if dca_names:
        for d in dca_data:
            if d["asset_name"] in dca_names:
                d["display_name"] = dca_names[d["asset_name"]]
    # Balances
    month_bals = db.query(MonthlyBalance).filter(MonthlyBalance.year == now.year, MonthlyBalance.month == now.month).all()
    bal_map = {}
    for mb in month_bals: bal_map[mb.account_id] = {"balance": mb.balance, "currency": "CNY"}
    # Portfolio
    holdings = {}
    for r in inv_rows:
        if r.type not in ("buy", "sell"): continue
        key = r.asset_name
        if key not in holdings: holdings[key] = {"asset_name": key, "asset_type": r.asset_type, "currency": r.currency, "quantity": 0, "total_cost": 0, "total_fees": 0}
        if r.type == "buy":
            holdings[key]["quantity"] += (r.quantity or 0)
            holdings[key]["total_cost"] += r.total_amount + (r.fees or 0)
            holdings[key]["total_fees"] += (r.fees or 0)
        else:
            holdings[key]["quantity"] -= (r.quantity or 0)
            holdings[key]["total_cost"] -= r.total_amount - (r.fees or 0)
    portfolio_holdings = []
    total_cost_cny = 0.0
    total_fees_raw = 0.0
    for h in holdings.values():
        if h["quantity"] <= 0: continue
        avg = h["total_cost"] / h["quantity"]
        total_cost_cny += h["total_cost"]
        total_fees_raw += h["total_fees"]
        portfolio_holdings.append({"asset_name": h["asset_name"], "asset_type": h["asset_type"], "currency": h["currency"], "quantity": round(h["quantity"], 4), "total_cost": round(h["total_cost"], 2), "avg_cost": round(avg, 2), "total_fees": round(h["total_fees"], 2)})

    price_cache = _load_portfolio_cache(db)
    total_value = 0.0
    has_prices = False
    for h in portfolio_holdings:
        cached = price_cache.get(h["asset_name"])
        if cached and "price" in cached:
            px = cached["price"]
            h["current_price"] = px
            h["current_value"] = round(h["quantity"] * px, 2)
            h["profit"] = round(h["current_value"] - h["total_cost"], 2)
            h["profit_pct"] = round(h["profit"] / h["total_cost"] * 100, 2) if h["total_cost"] > 0 else 0
            h["change_pct"] = cached.get("change_pct")
            h["display_name"] = cached.get("name")
            total_value += h["current_value"]
            has_prices = True

    # Latest cache date
    cache_date = None
    for h in portfolio_holdings:
        if h.get("current_price") is not None:
            cached = price_cache.get(h["asset_name"])
            if cached and cached.get("date"):
                if cache_date is None or cached["date"] > cache_date:
                    cache_date = cached["date"]

    summary = {
        "total_cost_cny": round(total_cost_cny, 2),
        "holdings_count": len(portfolio_holdings),
        "total_fees_cny": round(total_fees_raw, 2),
        "cache_date": cache_date,
    }
    if has_prices:
        summary["total_value_cny"] = round(total_value, 2)
        summary["total_profit_cny"] = round(total_value - total_cost_cny, 2)
        summary["total_profit_pct"] = round((total_value - total_cost_cny) / total_cost_cny * 100, 2) if total_cost_cny > 0 else 0
    else:
        summary["total_value_cny"] = None
        summary["total_profit_cny"] = None
        summary["total_profit_pct"] = None
        summary["daily_change_pct"] = None

    embedded = _json.dumps({
        "accounts": accounts_data, "investments": investments_data,
        "dca": dca_data, "balances": bal_map,
        "portfolio": {
            "holdings": portfolio_holdings,
            "total_cost_cny": round(total_cost_cny, 2),
            "summary": summary,
        },
        "today": now.strftime("%Y-%m-%d"),
    }, ensure_ascii=False)
    tpl = jinja_env.get_template("investment3.html")
    return HTMLResponse(tpl.render(nav="investment3", preload=embedded))

@app.get("/investment", response_class=HTMLResponse)
def page_investment(db: Session = Depends(get_db)):
    import json as _json

    # Accounts
    type_order = {"bank": 0, "credit": 1, "ewallet": 2, "investment": 3, "topup": 4, "loan": 5, "cash": 6}
    accounts = db.query(Account).filter(Account.is_active == 1, Account.parent_id == None).all()
    accounts.sort(key=lambda a: (type_order.get(a.type, 99), a.sort_order))
    accounts_data = [{"id": a.id, "name": a.name, "type": a.type, "currency": a.currency} for a in accounts]

    # Investment records
    inv_rows = db.query(InvestmentRecord).order_by(InvestmentRecord.date.desc()).all()
    investments_data = [{
        "id": r.id, "date": r.date, "type": r.type, "asset_name": r.asset_name,
        "asset_type": r.asset_type, "quantity": r.quantity, "price": r.price,
        "fees": r.fees, "total_amount": r.total_amount, "currency": r.currency,
        "platform": r.platform, "account_id": r.account_id, "notes": r.notes,
    } for r in inv_rows]

    # DCA plans
    dca_rows = db.query(DcaPlan).order_by(DcaPlan.is_active.desc(), DcaPlan.next_date).all()
    dca_data = [{
        "id": r.id, "asset_name": r.asset_name, "asset_type": r.asset_type,
        "amount": r.amount, "fees": r.fees, "currency": r.currency,
        "frequency": r.frequency, "next_date": r.next_date, "is_active": r.is_active,
        "platform": r.platform, "account_id": r.account_id, "payment_account": r.payment_account,
    } for r in dca_rows]

    # Balances (current month)
    now = datetime.now()
    month_bals = db.query(MonthlyBalance).filter(
        MonthlyBalance.year == now.year, MonthlyBalance.month == now.month
    ).all()
    bal_map = {}
    for mb in month_bals:
        acc = db.query(Account).filter(Account.id == mb.account_id).first()
        bal_map[mb.account_id] = {"balance": mb.balance, "currency": acc.currency if acc else "CNY"}

    # Portfolio holdings (from investment records, no external prices)
    holdings = {}
    for r in inv_rows:
        if r.type not in ("buy", "sell"): continue
        key = r.asset_name
        if key not in holdings:
            holdings[key] = {"asset_name": key, "asset_type": r.asset_type, "currency": r.currency, "quantity": 0, "total_cost": 0, "total_fees": 0}
        if r.type == "buy":
            holdings[key]["quantity"] += (r.quantity or 0)
            holdings[key]["total_cost"] += r.total_amount + (r.fees or 0)
            holdings[key]["total_fees"] += (r.fees or 0)
        else:
            holdings[key]["quantity"] -= (r.quantity or 0)
            holdings[key]["total_cost"] -= r.total_amount - (r.fees or 0)

    portfolio_holdings = []
    total_cost_cny = 0.0
    for h in holdings.values():
        if h["quantity"] <= 0: continue
        avg = h["total_cost"] / h["quantity"]
        conv = convert_to_cny(h["total_cost"], h["currency"], db)
        cost_cny = round(conv["value"], 2) if conv["valid"] else 0
        total_cost_cny += cost_cny
        portfolio_holdings.append({
            "asset_name": h["asset_name"], "asset_type": h["asset_type"],
            "currency": h["currency"], "quantity": round(h["quantity"], 4),
            "total_cost": round(h["total_cost"], 2), "avg_cost": round(avg, 2),
            "total_cost_cny": cost_cny, "total_fees": round(h["total_fees"], 2),
        })

    # Exchange rates
    rates_rows = db.query(ExchangeRate).all()
    rates_data = [{"from": r.from_currency, "to": r.to_currency, "rate": r.rate} for r in rates_rows]

    embedded = _json.dumps({
        "accounts": accounts_data,
        "investments": investments_data,
        "dca": dca_data,
        "balances": bal_map,
        "portfolio": {"holdings": portfolio_holdings, "total_cost_cny": round(total_cost_cny, 2)},
        "rates": rates_data,
        "today": now.strftime("%Y-%m-%d"),
    }, ensure_ascii=False)

    tpl = jinja_env.get_template("investment.html")
    return HTMLResponse(tpl.render(nav="investment", preload=embedded))


@app.get("/accounts", response_class=HTMLResponse)
def page_accounts():
    tpl = jinja_env.get_template("accounts.html")
    return HTMLResponse(tpl.render(nav="accounts"))


@app.get("/dashboard", response_class=HTMLResponse)
def page_dashboard():
    tpl = jinja_env.get_template("dashboard.html")
    return HTMLResponse(tpl.render(nav="dashboard"))


@app.get("/account/{acc_id}/records", response_class=HTMLResponse)
def page_account_records(acc_id: int):
    tpl = jinja_env.get_template("account_records.html")
    return HTMLResponse(tpl.render(nav="accounts", acc_id=acc_id))


@app.get("/backtest", response_class=HTMLResponse)
def page_backtest():
    tpl = jinja_env.get_template("backtest.html")
    return HTMLResponse(tpl.render(nav="backtest"))


# ── Account API ──────────────────────────────────────────────

@app.get("/api/accounts")
def api_accounts(db: Session = Depends(get_db)):
    """Return top-level accounts with merged sub-account info for multi-currency."""
    type_order = {"bank": 0, "credit": 1, "ewallet": 2, "investment": 3, "topup": 4, "loan": 5, "cash": 6}
    accounts = db.query(Account).filter(Account.parent_id == None).all()
    accounts.sort(key=lambda a: (type_order.get(a.type, 99), a.sort_order))
    result = []
    for a in accounts:
        subs = db.query(Account).filter(Account.parent_id == a.id).all()
        if subs:
            # Multi-currency: show parent with per-currency sub-accounts
            currencies = []
            total_cny = 0.0
            for s in subs:
                conv = convert_to_cny(1, s.currency, db)
                currencies.append({
                    "id": s.id, "currency": s.currency,
                    "rate": conv.get("rate"),
                    "valid": conv.get("valid", False),
                })
            result.append({
                "id": a.id, "name": a.name, "type": a.type,
                "currency": "MULTI", "is_active": a.is_active,
                "sort_order": a.sort_order, "notes": a.notes,
                "multi": True, "currencies": currencies,
            })
        else:
            result.append({
                "id": a.id, "name": a.name, "type": a.type,
                "currency": a.currency, "is_active": a.is_active,
                "sort_order": a.sort_order, "notes": a.notes,
                "multi": False,
            })
    return result


@app.post("/api/accounts")
def api_account_create(data: AccountCreate, db: Session = Depends(get_db)):
    currencies = data.currencies if data.currencies else [data.currency]

    # Adding a sub-account to an existing multi-currency parent
    if data.parent_id:
        parent = db.query(Account).filter(Account.id == data.parent_id).first()
        if parent:
            sub = Account(name=parent.name, type=parent.type, currency=data.currency,
                          parent_id=parent.id, is_active=1, sort_order=parent.sort_order, notes=parent.notes or "")
            db.add(sub)
            db.commit()
            db.refresh(sub)
            return {"id": sub.id, "name": sub.name, "currency": sub.currency, "parent_id": parent.id}

    if len(currencies) == 1 and not data.parent_id:
        a = Account(name=data.name, type=data.type, currency=currencies[0],
                    is_active=data.is_active, sort_order=data.sort_order, notes=data.notes)
        db.add(a)
        db.commit()
        db.refresh(a)
        return {"id": a.id, "name": a.name, "currency": a.currency}

    # Multi-currency: create parent + sub-accounts
    parent = Account(name=data.name, type=data.type, currency="MULTI",
                     is_active=data.is_active, sort_order=data.sort_order, notes=data.notes)
    db.add(parent)
    db.flush()
    for cur in currencies:
        sub = Account(name=data.name, type=data.type, currency=cur,
                      parent_id=parent.id, is_active=data.is_active,
                      sort_order=data.sort_order, notes=data.notes)
        db.add(sub)
    db.commit()
    db.refresh(parent)
    return {"id": parent.id, "name": parent.name, "currency": "MULTI", "currencies": currencies}


class AddCurrencyRequest(BaseModel):
    currency: str

@app.post("/api/accounts/{acc_id}/add-currency")
def api_account_add_currency(acc_id: int, data: AddCurrencyRequest, db: Session = Depends(get_db)):
    """Add a new currency to an account. Converts single-currency to multi if needed."""
    currency = data.currency.strip().upper()
    if not currency or len(currency) > 10:
        raise HTTPException(400, "无效币种")

    account = db.query(Account).filter(Account.id == acc_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")

    # If this account is a sub-account, operate on its parent
    if account.parent_id:
        acc_id = account.parent_id
        account = db.query(Account).filter(Account.id == acc_id).first()

    if account.currency != "MULTI":
        # Convert single-currency to multi-currency
        parent = Account(
            name=account.name, type=account.type, currency="MULTI",
            is_active=account.is_active, sort_order=account.sort_order,
            notes=account.notes or ""
        )
        db.add(parent)
        db.flush()
        # Move existing account as first sub
        account.parent_id = parent.id
        account.updated_at = _now()
        # Add new currency as second sub
        sub = Account(
            name=account.name, type=account.type, currency=currency,
            parent_id=parent.id, is_active=1, sort_order=account.sort_order,
            notes=account.notes or ""
        )
        db.add(sub)
        db.commit()
        return {"ok": True, "converted": True, "currency": currency, "parent_id": parent.id}
    else:
        # Already multi-currency: add new sub
        sub = Account(
            name=account.name, type=account.type, currency=currency,
            parent_id=account.id, is_active=1, sort_order=account.sort_order,
            notes=account.notes or ""
        )
        db.add(sub)
        db.commit()
        return {"ok": True, "converted": False, "currency": currency}


@app.put("/api/accounts/{acc_id}")
def api_account_update(acc_id: int, data: AccountUpdate, db: Session = Depends(get_db)):
    a = db.query(Account).filter(Account.id == acc_id).first()
    if not a:
        raise HTTPException(404, "账户不存在")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    a.updated_at = _now()
    db.commit()
    return {"ok": True}


@app.delete("/api/accounts/{acc_id}")
def api_account_delete(acc_id: int, db: Session = Depends(get_db)):
    a = db.query(Account).filter(Account.id == acc_id).first()
    if not a:
        raise HTTPException(404, "账户不存在")
    # Check if account has any real data
    has_balance = db.query(MonthlyBalance).filter(
        MonthlyBalance.account_id == acc_id, MonthlyBalance.balance != 0
    ).count() > 0
    has_investments = db.query(InvestmentRecord).filter(
        InvestmentRecord.account_id == acc_id
    ).count() > 0
    has_incomes = db.query(IncomeRecord).filter(
        IncomeRecord.account_id == acc_id
    ).count() > 0
    has_expenses = db.query(ExpenseRecord).filter(
        ExpenseRecord.account_id == acc_id
    ).count() > 0
    has_recurring = db.query(RecurringExpense).filter(
        RecurringExpense.payment_account == acc_id
    ).count() > 0
    has_dca = db.query(DcaPlan).filter(
        (DcaPlan.account_id == acc_id) | (DcaPlan.payment_account == acc_id)
    ).count() > 0

    if has_balance or has_investments or has_incomes or has_expenses or has_recurring or has_dca:
        a.is_active = 0
        a.updated_at = _now()
        db.commit()
        return {"ok": True, "soft": True}

    # No real data — safe to hard delete (also clean up zero-balance records)
    db.query(MonthlyBalance).filter(MonthlyBalance.account_id == acc_id).delete()
    db.delete(a)
    db.commit()
    return {"ok": True, "soft": False}


# ── Account Detail / Records API ──────────────────────────────

@app.get("/api/accounts/{acc_id}/records")
def api_account_records(acc_id: int, year: int = None, month: int = None, db: Session = Depends(get_db)):
    """Return all records linked to an account, grouped by type. Optional year/month filter.
    For multi-currency accounts, aggregates across all sub-accounts and includes per-currency breakdown."""
    a = db.query(Account).filter(Account.id == acc_id).first()
    if not a:
        raise HTTPException(404, "账户不存在")

    # Gather all related account IDs (self + sub-accounts)
    acc_ids = [acc_id]
    subs = db.query(Account).filter(Account.parent_id == acc_id).all()
    if subs:
        acc_ids += [s.id for s in subs]

    def _fmt_inv(r):
        return {"id": r.id, "date": r.date, "type": r.type, "asset": r.asset_name,
                "qty": r.quantity, "price": r.price, "amount": r.total_amount,
                "fees": r.fees, "currency": r.currency, "platform": r.platform, "notes": r.notes}

    def _fmt_inc(r):
        return {"id": r.id, "year": r.year, "month": r.month, "source": r.source,
                "amount": r.amount, "notes": r.notes}

    def _fmt_exp(r):
        return {"id": r.id, "datetime": r.datetime, "category": r.category,
                "amount": r.amount, "description": r.description, "notes": r.notes}

    def _fmt_dca(r):
        return {"id": r.id, "asset": r.asset_name, "amount": r.amount, "fees": r.fees,
                "currency": r.currency, "frequency": r.frequency, "next_date": r.next_date,
                "is_active": r.is_active}

    def _fmt_rec(r):
        return {"id": r.id, "description": r.description, "amount": r.amount,
                "category": r.category, "start": f"{r.start_year}-{r.start_month:02d}",
                "is_active": r.is_active}

    def _fmt_bal(r):
        return {"id": r.id, "year": r.year, "month": r.month, "balance": r.balance}

    ym_prefix = f"{year:04d}-{month:02d}" if year and month else None

    bal_q = db.query(MonthlyBalance).filter(MonthlyBalance.account_id.in_(acc_ids))
    inv_q = db.query(InvestmentRecord).filter(InvestmentRecord.account_id.in_(acc_ids))
    inc_q = db.query(IncomeRecord).filter(IncomeRecord.account_id.in_(acc_ids))
    exp_q = db.query(ExpenseRecord).filter(ExpenseRecord.account_id.in_(acc_ids))

    if ym_prefix:
        bal_q = bal_q.filter(MonthlyBalance.year == year, MonthlyBalance.month == month)
        inv_q = inv_q.filter(InvestmentRecord.date.like(f"{ym_prefix}%"))
        inc_q = inc_q.filter(IncomeRecord.year == year, IncomeRecord.month == month)
        exp_q = exp_q.filter(ExpenseRecord.datetime.like(f"{ym_prefix}%"))

    records = {
        "account": {"id": a.id, "name": a.name, "type": a.type, "currency": a.currency, "is_active": a.is_active},
        "balances": [_fmt_bal(r) for r in bal_q.order_by(MonthlyBalance.year.desc(), MonthlyBalance.month.desc()).all()],
        "investments": [_fmt_inv(r) for r in inv_q.order_by(InvestmentRecord.date.desc()).all()],
        "incomes": [_fmt_inc(r) for r in inc_q.order_by(IncomeRecord.year.desc(), IncomeRecord.month.desc()).all()],
        "expenses": [_fmt_exp(r) for r in exp_q.order_by(ExpenseRecord.datetime.desc()).all()],
        "dca_account": [_fmt_dca(r) for r in db.query(DcaPlan).filter(DcaPlan.account_id.in_(acc_ids)).all()],
        "dca_payment": [_fmt_dca(r) for r in db.query(DcaPlan).filter(DcaPlan.payment_account.in_(acc_ids)).all()],
        "recurring": [_fmt_rec(r) for r in db.query(RecurringExpense).filter(RecurringExpense.payment_account.in_(acc_ids)).all()],
    }
    # Per-currency breakdown for multi-currency accounts
    if subs:
        sub_data = []
        total_all_cny = 0.0
        for s in subs:
            conv = convert_to_cny(1, s.currency, db)
            rate = conv.get("rate")
            valid = conv.get("valid", False)
            # Get current month balance for this sub-account
            cur_bal = db.query(MonthlyBalance).filter(
                MonthlyBalance.account_id == s.id,
                MonthlyBalance.year == (year or datetime.now().year),
                MonthlyBalance.month == (month or datetime.now().month),
            ).first()
            bal = cur_bal.balance if cur_bal else 0.0
            cny_val = round(bal * rate, 2) if rate and valid else 0
            total_all_cny += cny_val
            sub_data.append({
                "sub_id": s.id, "currency": s.currency, "balance": bal,
                "rate": rate, "balance_cny": cny_val,
            })
        records["subs"] = sub_data
        records["total_cny"] = round(total_all_cny, 2)
    return records


def _investment_cash(acc_id: int, db):
    """Compute cash balance for an investment account from transaction history."""
    rows = db.query(InvestmentRecord).filter(InvestmentRecord.account_id == acc_id).all()
    cash = 0.0
    for r in rows:
        amount = r.total_amount or 0
        fees = r.fees or 0
        if r.type == "deposit":
            cash += amount
        elif r.type == "withdraw":
            cash -= amount
        elif r.type == "buy":
            cash -= (amount + fees)
        elif r.type == "sell":
            cash += (amount - fees)
        elif r.type == "dividend":
            cash += amount
    return round(cash, 2)


# ── Monthly Balance API ──────────────────────────────────────

@app.get("/api/balances")
def api_balances(year: int, month: int, db: Session = Depends(get_db)):
    # Only top-level accounts (not sub-accounts)
    type_order = {"bank": 0, "credit": 1, "ewallet": 2, "investment": 3, "topup": 4, "loan": 5, "cash": 6}
    accounts = db.query(Account).filter(
        Account.is_active == 1, Account.parent_id == None
    ).all()
    accounts.sort(key=lambda a: (type_order.get(a.type, 99), a.sort_order))

    all_balances = {
        mb.account_id: mb
        for mb in db.query(MonthlyBalance).filter(
            MonthlyBalance.year == year, MonthlyBalance.month == month
        ).all()
    }
    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    prev_balances = {
        mb.account_id: mb.balance
        for mb in db.query(MonthlyBalance).filter(
            MonthlyBalance.year == prev_year, MonthlyBalance.month == prev_month
        ).all()
    }

    result = []
    total_cny = 0.0
    for a in accounts:
        subs = db.query(Account).filter(Account.parent_id == a.id).all()

        if subs:
            # Multi-currency: sum sub-account balances
            sub_items = []
            merged_balance_cny = 0.0
            for s in subs:
                cur = all_balances.get(s.id)
                bal = cur.balance if cur else prev_balances.get(s.id, 0.0)
                conv = convert_to_cny(bal, s.currency, db)
                cny_val = round(conv["value"], 2) if conv["valid"] else 0
                merged_balance_cny += cny_val
                sub_items.append({
                    "sub_id": s.id, "currency": s.currency,
                    "balance": bal, "balance_cny": round(conv["value"], 2) if conv["valid"] else None,
                    "rate": conv["rate"], "valid_currency": conv["valid"],
                    "has_record": cur is not None,
                })
            total_cny += merged_balance_cny
            result.append({
                "account_id": a.id, "account_name": a.name,
                "type": a.type, "currency": "MULTI",
                "balance": merged_balance_cny,
                "balance_cny": merged_balance_cny,
                "rate": None, "valid_currency": True,
                "prev_balance": None, "has_record": any(s["has_record"] for s in sub_items),
                "multi": True, "subs": sub_items,
            })
        else:
            cur = all_balances.get(a.id)
            is_inv = a.type == "investment"
            balance = cur.balance if cur else prev_balances.get(a.id, 0.0)
            has_record = cur is not None
            conv = convert_to_cny(balance, a.currency, db)
            if conv["valid"] and conv["rate"] is not None and not is_inv:
                total_cny += conv["value"]
            result.append({
                "account_id": a.id, "account_name": a.name,
                "type": a.type, "currency": a.currency,
                "balance": balance,
                "balance_cny": round(conv["value"], 2) if conv["valid"] else None,
                "rate": conv["rate"], "valid_currency": conv["valid"],
                "prev_balance": prev_balances.get(a.id),
                "has_record": has_record,
                "multi": False,
                "is_investment": is_inv,
            })
    return {"items": result, "total_cny": round(total_cny, 2), "year": year, "month": month}


@app.put("/api/balances")
def api_balances_save(data: MonthlyBalanceSave, db: Session = Depends(get_db)):
    neg_ok = {"credit", "loan"}
    for entry in data.balances:
        if entry.balance < 0:
            acc = db.query(Account).filter(Account.id == entry.account_id).first()
            if acc and acc.type not in neg_ok:
                raise HTTPException(400, f"「{acc.name}」不支持负数余额")
        mb = db.query(MonthlyBalance).filter(
            MonthlyBalance.account_id == entry.account_id,
            MonthlyBalance.year == data.year,
            MonthlyBalance.month == data.month,
        ).first()
        if mb:
            mb.balance = entry.balance
            mb.updated_at = _now()
        else:
            db.add(MonthlyBalance(
                account_id=entry.account_id,
                year=data.year, month=data.month,
                balance=entry.balance,
            ))
    db.commit()
    return {"ok": True}


@app.get("/api/balances/history")
def api_balances_history(account_id: int, db: Session = Depends(get_db)):
    rows = db.query(MonthlyBalance).filter(
        MonthlyBalance.account_id == account_id
    ).order_by(MonthlyBalance.year, MonthlyBalance.month).all()
    return [{"year": r.year, "month": r.month, "balance": r.balance} for r in rows]


# ── Income API ───────────────────────────────────────────────

@app.get("/api/incomes")
def api_incomes(year: int, month: int, db: Session = Depends(get_db)):
    rows = db.query(IncomeRecord).filter(
        IncomeRecord.year == year, IncomeRecord.month == month
    ).all()
    return [{
        "id": r.id, "year": r.year, "month": r.month,
        "source": r.source, "amount": r.amount,
        "account_id": r.account_id, "notes": r.notes,
    } for r in rows]


@app.post("/api/incomes")
def api_income_create(data: IncomeCreate, db: Session = Depends(get_db)):
    r = IncomeRecord(**data.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return {"id": r.id, "source": r.source, "amount": r.amount}


@app.put("/api/incomes/{inc_id}")
def api_income_update(inc_id: int, data: IncomeUpdate, db: Session = Depends(get_db)):
    r = db.query(IncomeRecord).filter(IncomeRecord.id == inc_id).first()
    if not r:
        raise HTTPException(404, "记录不存在")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    r.updated_at = _now()
    db.commit()
    return {"ok": True}


@app.delete("/api/incomes/{inc_id}")
def api_income_delete(inc_id: int, db: Session = Depends(get_db)):
    r = db.query(IncomeRecord).filter(IncomeRecord.id == inc_id).first()
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


# ── Expense API ──────────────────────────────────────────────

@app.get("/api/expenses")
def api_expenses(
    year: int = None, month: int = None,
    category: str = None, account_id: int = None,
    db: Session = Depends(get_db),
):
    if year is not None and month is not None:
        ensure_expenses_for_month(year, month, db)
    q = db.query(ExpenseRecord)
    if year is not None and month is not None:
        prefix = f"{year:04d}-{month:02d}"
        q = q.filter(ExpenseRecord.datetime.like(f"{prefix}%"))
    elif year is not None:
        q = q.filter(ExpenseRecord.datetime.like(f"{year:04d}-%"))
    if category:
        q = q.filter(ExpenseRecord.category == category)
    if account_id:
        q = q.filter(ExpenseRecord.account_id == account_id)
    rows = q.order_by(ExpenseRecord.datetime.desc()).all()
    return [{
        "id": r.id, "datetime": r.datetime,
        "account_id": r.account_id, "category": r.category,
        "amount": r.amount, "description": r.description,
        "recurring_id": r.recurring_id, "notes": r.notes,
    } for r in rows]


@app.post("/api/expenses")
def api_expense_create(data: ExpenseCreate, db: Session = Depends(get_db)):
    r = ExpenseRecord(**data.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    existing = db.query(ExpenseCategory).filter(ExpenseCategory.name == data.category).first()
    if not existing:
        db.add(ExpenseCategory(name=data.category, sort_order=99, is_preset=0))
        db.commit()
    return {"id": r.id, "category": r.category, "amount": r.amount}


@app.put("/api/expenses/{exp_id}")
def api_expense_update(exp_id: int, data: ExpenseUpdate, db: Session = Depends(get_db)):
    r = db.query(ExpenseRecord).filter(ExpenseRecord.id == exp_id).first()
    if not r:
        raise HTTPException(404, "记录不存在")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    r.updated_at = _now()
    db.commit()
    return {"ok": True}


@app.delete("/api/expenses/{exp_id}")
def api_expense_delete(exp_id: int, db: Session = Depends(get_db)):
    r = db.query(ExpenseRecord).filter(ExpenseRecord.id == exp_id).first()
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


# ── Category API ─────────────────────────────────────────────

@app.get("/api/categories")
def api_categories(db: Session = Depends(get_db)):
    rows = db.query(ExpenseCategory).order_by(ExpenseCategory.sort_order).all()
    return [{"id": r.id, "name": r.name, "is_preset": r.is_preset} for r in rows]


@app.post("/api/categories")
def api_category_create(data: CategoryCreate, db: Session = Depends(get_db)):
    existing = db.query(ExpenseCategory).filter(ExpenseCategory.name == data.name).first()
    if existing:
        return {"id": existing.id, "name": existing.name, "exists": True}
    c = ExpenseCategory(name=data.name, sort_order=99, is_preset=0)
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "name": c.name}


# ── Recurring Expense API ────────────────────────────────────

@app.get("/api/recurring")
def api_recurring(db: Session = Depends(get_db)):
    rows = db.query(RecurringExpense).order_by(
        RecurringExpense.is_active.desc(), RecurringExpense.start_year
    ).all()
    return [{
        "id": r.id, "description": r.description,
        "amount": r.amount, "category": r.category,
        "start_year": r.start_year, "start_month": r.start_month,
        "end_year": r.end_year, "end_month": r.end_month,
        "payment_account": r.payment_account,
        "is_active": r.is_active, "notes": r.notes,
    } for r in rows]


@app.post("/api/recurring")
def api_recurring_create(data: RecurringCreate, db: Session = Depends(get_db)):
    r = RecurringExpense(**data.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    # Auto-generate expense records for all applicable months from start to now
    now = datetime.now()
    end_year, end_month = data.end_year, data.end_month
    if end_year is None:
        end_year, end_month = now.year, now.month
    for y in range(data.start_year, end_year + 1):
        m_start = data.start_month if y == data.start_year else 1
        m_end = end_month if y == end_year else 12
        for m in range(m_start, m_end + 1):
            if y > now.year or (y == now.year and m > now.month):
                break
            ensure_expenses_for_month(y, m, db)
    return {"id": r.id, "description": r.description, "generated": True}


@app.put("/api/recurring/{rec_id}")
def api_recurring_update(rec_id: int, data: RecurringUpdate, db: Session = Depends(get_db)):
    r = db.query(RecurringExpense).filter(RecurringExpense.id == rec_id).first()
    if not r:
        raise HTTPException(404, "记录不存在")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    r.updated_at = _now()
    db.commit()
    return {"ok": True}


@app.delete("/api/recurring/{rec_id}")
def api_recurring_delete(rec_id: int, hard: bool = False, db: Session = Depends(get_db)):
    r = db.query(RecurringExpense).filter(RecurringExpense.id == rec_id).first()
    if not r:
        raise HTTPException(404, "记录不存在")
    if hard:
        # Also delete auto-generated expense records
        db.query(ExpenseRecord).filter(ExpenseRecord.recurring_id == rec_id).delete()
        db.delete(r)
    else:
        r.is_active = 0
        r.updated_at = _now()
    db.commit()
    return {"ok": True, "hard": hard}


# ── DCA Plan API ────────────────────────────────────────

@app.get("/api/dca-plans")
def api_dca_plans(db: Session = Depends(get_db)):
    rows = db.query(DcaPlan).order_by(DcaPlan.is_active.desc(), DcaPlan.next_date).all()
    return [{
        "id": r.id, "asset_name": r.asset_name, "asset_type": r.asset_type,
        "amount": r.amount, "fees": r.fees, "currency": r.currency,
        "platform": r.platform, "account_id": r.account_id,
        "payment_account": r.payment_account, "frequency": r.frequency,
        "start_date": r.start_date, "next_date": r.next_date,
        "is_active": r.is_active, "notes": r.notes,
    } for r in rows]


@app.post("/api/dca-plans")
def api_dca_create(data: dict, db: Session = Depends(get_db)):
    plan = DcaPlan(
        asset_name=data["asset_name"], asset_type=data.get("asset_type", "etf"),
        amount=data["amount"], fees=data.get("fees", 0),
        currency=data.get("currency", "CNY"), platform=data.get("platform", ""),
        account_id=data.get("account_id"), payment_account=data.get("payment_account"),
        frequency=data.get("frequency", "monthly"), start_date=data.get("start_date"),
        next_date=data["next_date"], notes=data.get("notes", ""),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return {"id": plan.id, "asset_name": plan.asset_name}


@app.put("/api/dca-plans/{plan_id}")
def api_dca_update(plan_id: int, data: dict, db: Session = Depends(get_db)):
    plan = db.query(DcaPlan).filter(DcaPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "定投计划不存在")
    for k, v in data.items():
        if hasattr(plan, k):
            setattr(plan, k, v)
    plan.updated_at = _now()
    db.commit()
    return {"ok": True}


@app.delete("/api/dca-plans/{plan_id}")
def api_dca_delete(plan_id: int, hard: bool = False, db: Session = Depends(get_db)):
    plan = db.query(DcaPlan).filter(DcaPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "定投计划不存在")
    if hard:
        # Also delete associated investment records
        db.query(InvestmentRecord).filter(
            InvestmentRecord.asset_name == plan.asset_name,
            InvestmentRecord.notes.like("%定投%")
        ).delete()
        db.delete(plan)
    else:
        plan.is_active = 0
        plan.updated_at = _now()
    db.commit()
    return {"ok": True, "hard": hard}


@app.post("/api/dca-plans/{plan_id}/execute")
def api_dca_execute(plan_id: int, price: float | None = None, note: str | None = None, db: Session = Depends(get_db)):
    """Manually execute a DCA plan — creates an investment record.

    price: if None, try to fetch current market price.
    Fees are deducted from the amount: invest_amount = amount - fees.
    """
    plan = db.query(DcaPlan).filter(DcaPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "定投计划不存在")

    # Check investment account balance
    balance_ok = True
    if plan.account_id:
        now_dt = datetime.now()
        acct_bal = db.query(MonthlyBalance).filter(
            MonthlyBalance.account_id == plan.account_id,
            MonthlyBalance.year == now_dt.year,
            MonthlyBalance.month == now_dt.month,
        ).first()
        if acct_bal and acct_bal.balance < plan.amount:
            balance_ok = False

    resolved_price = price
    if not resolved_price:
        p, _, _, _ = _fetch_price(plan.asset_name, plan.currency, db, plan.asset_type)
        if p:
            resolved_price = p

    fee = plan.fees or 0
    invest_amount = plan.amount - fee  # fees deducted from amount
    qty = round(invest_amount / resolved_price, 4) if resolved_price and invest_amount > 0 else None

    notes_parts = [f"定投: {plan.asset_name} ({plan.frequency})"]
    if not balance_ok:
        notes_parts.append("⚠ 扣款账户余额不足")
    if note:
        notes_parts.append(note)

    inv = InvestmentRecord(
        date=plan.next_date,
        type="buy",
        asset_name=plan.asset_name,
        asset_type=plan.asset_type,
        quantity=qty,
        price=resolved_price,
        fees=fee,
        total_amount=invest_amount,
        currency=plan.currency,
        platform=plan.platform,
        account_id=plan.account_id,
        notes=" | ".join(notes_parts),
    )
    db.add(inv)
    # Save price to market_prices for DCA display
    if resolved_price:
        existing_mp = db.query(MarketPrice).filter(
            MarketPrice.ticker == plan.asset_name, MarketPrice.date == plan.next_date
        ).first()
        if existing_mp:
            existing_mp.close_price = resolved_price
            existing_mp.source = "dca"
            existing_mp.updated_at = _now()
        else:
            db.add(MarketPrice(ticker=plan.asset_name, date=plan.next_date,
                               close_price=resolved_price, source="dca", updated_at=_now()))
    # Advance next_date
    from datetime import datetime as dt, timedelta
    nd = dt.strptime(plan.next_date, "%Y-%m-%d")
    if plan.frequency == "daily":
        nd += timedelta(days=1)
    elif plan.frequency == "weekly":
        nd += timedelta(days=7)
    elif plan.frequency == "biweekly":
        nd += timedelta(days=14)
    else:
        m = nd.month + 1
        y = nd.year
        if m > 12: m = 1; y += 1
        nd = nd.replace(year=y, month=m)
    plan.next_date = nd.strftime("%Y-%m-%d")
    plan.updated_at = _now()
    db.commit()
    return {"ok": True, "investment_id": inv.id, "next_date": plan.next_date, "balance_ok": balance_ok}


@app.post("/api/dca-plans/{plan_id}/backfill")
def _resolve_market(asset_type: str) -> str:
    """Map asset type to market for trading calendar."""
    if asset_type == "fund": return "CN"
    return "US"  # default for stock/etf/crypto


def _is_trading_day(date_str: str, market: str, db: Session) -> bool:
    """Check if date is a trading day. Caches result in DB."""
    existing = db.query(TradingCalendar).filter(
        TradingCalendar.date == date_str, TradingCalendar.market == market
    ).first()
    if existing is not None:
        return bool(existing.is_trading)

    # Check via akshare
    try:
        import akshare as ak
        if market == "US":
            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                trade_dates = set(df['trade_date'].tolist())
            else:
                # Fallback: assume Mon-Fri are trading days
                from datetime import datetime as _dt
                d = _dt.strptime(date_str, "%Y-%m-%d")
                trade_dates = set() if d.weekday() >= 5 else {date_str}
        else:  # CN
            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                trade_dates = set(df['trade_date'].tolist())
            else:
                from datetime import datetime as _dt
                d = _dt.strptime(date_str, "%Y-%m-%d")
                trade_dates = set() if d.weekday() >= 5 else {date_str}
    except Exception:
        # If akshare fails, assume Mon-Fri are trading days
        from datetime import datetime as _dt
        d = _dt.strptime(date_str, "%Y-%m-%d")
        is_trade = d.weekday() < 5
        db.add(TradingCalendar(date=date_str, market=market, is_trading=1 if is_trade else 0))
        db.commit()
        return is_trade

    is_trade = 1 if date_str in trade_dates else 0
    db.add(TradingCalendar(date=date_str, market=market, is_trading=is_trade))
    db.commit()
    return bool(is_trade)


@app.post("/api/market-prices")
def api_market_prices_create(data: dict, db: Session = Depends(get_db)):
    """Save a manual price entry to market_prices table."""
    ticker = data.get("ticker")
    date_val = data.get("date")
    close = data.get("close_price")
    if not ticker or not date_val or close is None:
        raise HTTPException(400, "缺少字段")
    existing = db.query(MarketPrice).filter(
        MarketPrice.ticker == ticker, MarketPrice.date == date_val
    ).first()
    if existing:
        existing.close_price = close
        existing.source = data.get("source", "manual")
        existing.updated_at = _now()
    else:
        db.add(MarketPrice(ticker=ticker, date=date_val, close_price=close,
                           source=data.get("source", "manual"), updated_at=_now()))
    db.commit()
    return {"ok": True}


@app.get("/api/refresh-dca-prices")
def api_refresh_dca_prices(tickers: str = "", db: Session = Depends(get_db)):
    """Refresh prices for DCA assets even if they have no holdings."""
    from datetime import date as dt_date
    today = dt_date.today().isoformat()
    results = {}
    for symbol in tickers.split(","):
        symbol = symbol.strip()
        if not symbol: continue
        try:
            plan = db.query(DcaPlan).filter(DcaPlan.asset_name == symbol).first()
            asset_type = plan.asset_type if plan else "stock"
            px, _, name, chg = _fetch_price(symbol, "USD", db, asset_type)
            if px is not None:
                existing = db.query(MarketPrice).filter(
                    MarketPrice.ticker == symbol, MarketPrice.date == today
                ).first()
                if existing:
                    existing.close_price = px
                    existing.change_pct = chg
                    existing.name = name
                    existing.source = "dca-refresh"
                    existing.updated_at = _now()
                else:
                    db.add(MarketPrice(ticker=symbol, date=today, close_price=px,
                                       change_pct=chg, name=name, source="dca-refresh", updated_at=_now()))
                db.commit()
                results[symbol] = {"price": px, "name": name, "ok": True}
            else:
                results[symbol] = {"price": None, "name": None, "ok": False, "error": "no data"}
        except Exception as e:
            results[symbol] = {"price": None, "name": None, "ok": False, "error": str(e)}
    return {"ok": True, "results": results}


@app.get("/api/market-status")
def api_market_status():
    """Return market status. after_close = true only when market has closed for the day."""
    from datetime import timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    et = now_utc + timedelta(hours=-4)  # EDT
    # After 4:00 PM ET = market closed for the day
    market_close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    after_close = et >= market_close and et.weekday() < 5
    return {"open": _is_market_open(), "closed": after_close}


def api_dca_backfill(plan_id: int, start_date: str = None, holiday: str = "skip", db: Session = Depends(get_db)):
    """Generate historical buy records for a DCA plan from start_date to today."""
    plan = db.query(DcaPlan).filter(DcaPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "定投计划不存在")

    from datetime import datetime as dt, timedelta

    start = dt.strptime(start_date or plan.next_date, "%Y-%m-%d")
    market = _resolve_market(plan.asset_type)
    end = dt.now()
    # Don't backfill today if market hasn't closed yet
    if not _is_market_open():
        end = end - timedelta(days=1)
    if start >= end:
        return {"ok": True, "count": 0, "msg": "无需补投"}

    # Generate trading dates based on frequency
    dates = []
    cur = start
    while cur <= end:
        if plan.frequency == "daily":
            cur += timedelta(days=1)
        elif plan.frequency == "weekly":
            cur += timedelta(days=7)
        elif plan.frequency == "biweekly":
            cur += timedelta(days=14)
        else:  # monthly
            m = cur.month + 1
            y = cur.year
            if m > 12:
                m = 1
                y += 1
            cur = cur.replace(year=y, month=m)
        if cur <= end:
            date_str = cur.strftime("%Y-%m-%d")
            if not _is_trading_day(date_str, market, db):
                if holiday == "skip":
                    continue  # skip this period entirely
                elif holiday == "next":
                    # Advance to next trading day
                    nxt = cur
                    for _ in range(10):
                        nxt += timedelta(days=1)
                        if _is_trading_day(nxt.strftime("%Y-%m-%d"), market, db):
                            date_str = nxt.strftime("%Y-%m-%d")
                            break
                        if nxt > end:
                            continue
            dates.append(date_str)

    if not dates:
        return {"ok": True, "count": 0, "msg": "无需补投"}

    # Get prices: try DB cache first, then akshare
    prices = {}
    # 1. Try market_prices table
    for d in dates:
        mp = db.query(MarketPrice).filter(
            MarketPrice.ticker == plan.asset_name, MarketPrice.date == d
        ).first()
        if mp and mp.close_price > 0:
            prices[d] = mp.close_price

    # 2. Try akshare for missing dates
    missing = [d for d in dates if d not in prices]
    if missing:
        try:
            import akshare as ak
            df = ak.stock_us_hist(symbol=plan.asset_name, period="daily",
                                  start_date=missing[0].replace("-",""),
                                  end_date=missing[-1].replace("-",""))
            if df is not None and not df.empty:
                for d in missing:
                    row = df[df['日期'] == d]
                    if not row.empty:
                        px = float(row['收盘'].iloc[0])
                        if px > 0:
                            prices[d] = round(px, 2)
            print(f"[backfill] akshare: got {len([d for d in missing if d in prices])} prices for {plan.asset_name}")
        except Exception as e:
            print(f"[backfill] akshare failed: {e}")

    # Check for existing records to avoid duplicates
    existing_dates = set()
    existing = db.query(InvestmentRecord).filter(
        InvestmentRecord.asset_name == plan.asset_name,
        InvestmentRecord.type == "buy",
        InvestmentRecord.notes.like("%定投补投%")
    ).all()
    for e in existing:
        existing_dates.add(e.date)

    # Create records
    count = 0
    for d in dates:
        if d in existing_dates:
            continue
        px = prices.get(d)
        if not px or px <= 0:
            continue
        fee = plan.fees or 0
        invest_amount = plan.amount - fee
        qty = round(invest_amount / px, 4) if invest_amount > 0 else 0
        notes_parts = [f"定投补投: {plan.asset_name} ({plan.frequency})"]
        db.add(InvestmentRecord(
            date=d, type="buy", asset_name=plan.asset_name,
            asset_type=plan.asset_type, quantity=qty, price=px,
            fees=fee, total_amount=invest_amount,
            currency=plan.currency, platform=plan.platform,
            account_id=plan.account_id,
            notes=" | ".join(notes_parts),
        ))
        count += 1
    db.commit()
    return {"ok": True, "count": count, "dates": len(dates), "priced": len(prices)}


@app.delete("/api/balances/{bal_id}")
def api_balance_delete(bal_id: int, db: Session = Depends(get_db)):
    r = db.query(MonthlyBalance).filter(MonthlyBalance.id == bal_id).first()
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


@app.delete("/api/accounts/{acc_id}/records")
def api_account_records_clear(acc_id: int, db: Session = Depends(get_db)):
    """Delete ALL related records for an account (keeps the account itself).
    Also clears sub-accounts for multi-currency parent accounts."""
    acc_ids = [acc_id]
    subs = db.query(Account).filter(Account.parent_id == acc_id).all()
    if subs:
        acc_ids += [s.id for s in subs]

    db.query(MonthlyBalance).filter(MonthlyBalance.account_id.in_(acc_ids)).delete()
    db.query(InvestmentRecord).filter(InvestmentRecord.account_id.in_(acc_ids)).delete()
    db.query(IncomeRecord).filter(IncomeRecord.account_id.in_(acc_ids)).delete()
    db.query(ExpenseRecord).filter(ExpenseRecord.account_id.in_(acc_ids)).delete()
    db.query(DcaPlan).filter(
        (DcaPlan.account_id.in_(acc_ids)) | (DcaPlan.payment_account.in_(acc_ids))
    ).delete()
    db.query(RecurringExpense).filter(RecurringExpense.payment_account.in_(acc_ids)).delete()
    db.commit()
    return {"ok": True}


# ── Account Detail API ──────────────────────────────────

@app.get("/api/accounts/{acc_id}/detail")
def api_account_detail(acc_id: int, db: Session = Depends(get_db)):
    """Get account info + balance history + related transactions."""
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(404, "账户不存在")

    # Balance history
    balances = db.query(MonthlyBalance).filter(
        MonthlyBalance.account_id == acc_id
    ).order_by(MonthlyBalance.year, MonthlyBalance.month).all()
    balance_history = []
    for mb in balances:
        conv = convert_to_cny(mb.balance, acc.currency, db)
        balance_history.append({
            "year": mb.year, "month": mb.month,
            "balance": mb.balance,
            "balance_cny": round(conv["value"], 2) if conv["valid"] else None,
        })

    # Related expenses
    expenses = db.query(ExpenseRecord).filter(
        ExpenseRecord.account_id == acc_id
    ).order_by(ExpenseRecord.datetime.desc()).limit(50).all()

    # Related investments
    investments = db.query(InvestmentRecord).filter(
        InvestmentRecord.account_id == acc_id
    ).order_by(InvestmentRecord.date.desc()).limit(50).all()

    # Related incomes
    incomes = db.query(IncomeRecord).filter(
        IncomeRecord.account_id == acc_id
    ).order_by(IncomeRecord.year.desc(), IncomeRecord.month.desc()).limit(50).all()

    return {
        "account": {
            "id": acc.id, "name": acc.name, "type": acc.type,
            "currency": acc.currency, "is_active": acc.is_active,
        },
        "balance_history": balance_history,
        "expenses": [{"datetime": e.datetime, "category": e.category, "amount": e.amount, "description": e.description} for e in expenses],
        "investments": [{"date": i.date, "type": i.type, "asset_name": i.asset_name, "total_amount": i.total_amount, "currency": i.currency} for i in investments],
        "incomes": [{"year": i.year, "month": i.month, "source": i.source, "amount": i.amount} for i in incomes],
    }

@app.get("/api/investments")
# ── Investment API ───────────────────────────────────────────

@app.get("/api/investments")
def api_investments(
    year: int = None, type: str = None, platform: str = None,
    db: Session = Depends(get_db),
):
    q = db.query(InvestmentRecord)
    if year:
        q = q.filter(InvestmentRecord.date.like(f"{year:04d}-%"))
    if type:
        q = q.filter(InvestmentRecord.type == type)
    if platform:
        q = q.filter(InvestmentRecord.platform == platform)
    rows = q.order_by(InvestmentRecord.date.desc()).all()
    return [{
        "id": r.id, "date": r.date, "type": r.type,
        "asset_name": r.asset_name, "asset_type": r.asset_type,
        "quantity": r.quantity, "price": r.price,
        "total_amount": r.total_amount, "currency": r.currency,
        "platform": r.platform, "account_id": r.account_id, "notes": r.notes,
    } for r in rows]


@app.post("/api/investments")
def api_investment_create(data: InvestmentCreate, db: Session = Depends(get_db)):
    d = data.model_dump()
    # Auto-fill missing quantity/price for buy/sell
    if data.type in ("buy", "sell"):
        qty = d.get("quantity")
        price = d.get("price")
        total = d["total_amount"]
        if not qty and price and price > 0:
            qty = round(total / price, 4)
        elif not price and qty and qty > 0:
            price = round(total / qty, 2)
        d["quantity"] = qty
        d["price"] = price
    r = InvestmentRecord(**{k: v for k, v in d.items() if k in [c.name for c in InvestmentRecord.__table__.columns]})
    db.add(r)
    db.commit()
    db.refresh(r)
    return {"id": r.id, "asset_name": r.asset_name, "type": r.type, "quantity": r.quantity, "price": r.price}


@app.put("/api/investments/{inv_id}")
def api_investment_update(inv_id: int, data: InvestmentUpdate, db: Session = Depends(get_db)):
    r = db.query(InvestmentRecord).filter(InvestmentRecord.id == inv_id).first()
    if not r:
        raise HTTPException(404, "记录不存在")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    r.updated_at = _now()
    db.commit()
    return {"ok": True}


@app.delete("/api/investments/{inv_id}")
def api_investment_delete(inv_id: int, db: Session = Depends(get_db)):
    r = db.query(InvestmentRecord).filter(InvestmentRecord.id == inv_id).first()
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


# ── Portfolio API ───────────────────────────────────────

_portfolio_full_cache = None

def _is_market_open() -> bool:
    from datetime import timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    et_offset = timedelta(hours=-4)
    now_et = now_utc + et_offset
    if now_et.weekday() >= 5:
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close

def _load_portfolio_cache(db: Session):
    rows = db.query(MarketPrice).all()
    latest = {}
    for r in rows:
        if r.ticker not in latest or r.date > latest[r.ticker]["date"]:
            latest[r.ticker] = {"ts": 0, "price": r.close_price, "name": r.name, "change_pct": r.change_pct, "date": r.date}
    return {t: {"ts": v["ts"], "price": v["price"], "name": v["name"], "change_pct": v["change_pct"]} for t, v in latest.items()}

def _save_portfolio_cache(cache: dict, db: Session):
    from datetime import date
    today = date.today().isoformat()
    for ticker, info in cache.items():
        price = info.get("price")
        if price is None: continue
        existing = db.query(MarketPrice).filter(MarketPrice.ticker == ticker, MarketPrice.date == today).first()
        if existing:
            existing.close_price = price
            existing.change_pct = info.get("change_pct")
            existing.name = info.get("name")
            existing.source = "api"
            existing.updated_at = _now()
        else:
            db.add(MarketPrice(ticker=ticker, date=today, close_price=price, change_pct=info.get("change_pct"), name=info.get("name"), source="api", updated_at=_now()))
    db.commit()

@app.get("/api/portfolio")
def api_portfolio(refresh: bool = False, db: Session = Depends(get_db)):
    """Aggregated holdings by asset. Optionally fetches live prices."""
    rows = db.query(InvestmentRecord).order_by(InvestmentRecord.date).all()
    holdings = {}  # asset_name → {qty, total_cost, currency, ...}
    for r in rows:
        key = r.asset_name
        if key not in holdings:
            holdings[key] = {"asset_name": key, "asset_type": r.asset_type, "currency": r.currency, "quantity": 0, "total_cost": 0}
        if r.type == "buy":
            holdings[key]["quantity"] += (r.quantity or 0)
            holdings[key]["total_cost"] += r.total_amount + (r.fees or 0)
        elif r.type == "sell":
            holdings[key]["quantity"] -= (r.quantity or 0)
            holdings[key]["total_cost"] -= r.total_amount - (r.fees or 0)

    # Load price cache
    cache = {} if refresh else _load_portfolio_cache(db)
    need_refresh = refresh

    result = []
    for h in holdings.values():
        if h["quantity"] <= 0:
            continue
        avg_cost = h["total_cost"] / h["quantity"] if h["quantity"] > 0 else 0
        item = {
            "asset_name": h["asset_name"],
            "asset_type": h["asset_type"],
            "currency": h["currency"],
            "display_name": None,
            "quantity": round(h["quantity"], 4),
            "total_cost": round(h["total_cost"], 2),
            "avg_cost": round(avg_cost, 2),
            "current_price": None,
            "current_value": None,
            "profit": None,
            "profit_pct": None,
            "change_pct": None,
        }
        ticker = h["asset_name"]
        cached = cache.get(ticker)
        # Only fetch external prices when user explicitly clicks "refresh"
        should_fetch = need_refresh

        if should_fetch:
            price, price_cny, name, change_pct = _fetch_price(
                ticker, h["currency"], db, h["asset_type"]
            )
            if price is not None:
                cache[ticker] = {"ts": datetime.now().timestamp(), "price": price,
                                 "name": name, "change_pct": change_pct}
            elif cached:
                price, name, change_pct = cached["price"], cached.get("name"), cached.get("change_pct")
            else:
                price = None
        elif cached:
            price = cached["price"]
            name = cached.get("name")
            change_pct = cached.get("change_pct")
        else:
            price = None

        if price is not None:
            item["display_name"] = name
            item["current_price"] = price
            item["current_value"] = round(h["quantity"] * price, 2)
            item["profit"] = round(item["current_value"] - h["total_cost"], 2)
            if h["total_cost"] > 0:
                item["profit_pct"] = round(item["profit"] / h["total_cost"] * 100, 2)
            if change_pct is not None:
                item["change_pct"] = change_pct
        result.append(item)

    if cache:
        _save_portfolio_cache(cache, db)

    # ── Portfolio summary (all values in CNY) ────────
    total_cost_cny = 0
    total_value_cny = 0
    daily_pnl_cny = 0
    for it in result:
        # Convert cost to CNY
        if it["currency"] == "CNY":
            total_cost_cny += it["total_cost"]
        else:
            conv = convert_to_cny(it["total_cost"], it["currency"], db)
            if conv["valid"] and conv["rate"] is not None:
                total_cost_cny += conv["value"]
        # Convert current value to CNY
        if it["current_value"] is not None:
            if it["currency"] == "CNY":
                val_cny = it["current_value"]
            else:
                conv = convert_to_cny(it["current_value"], it["currency"], db)
                val_cny = conv["value"] if conv["valid"] and conv["rate"] is not None else 0
            total_value_cny += val_cny
            # Daily P&L contribution
            if it["change_pct"] is not None:
                daily_pnl_cny += val_cny * it["change_pct"] / 100

    # Sum fees from buy/sell records, convert to CNY
    total_fees_cny = 0.0
    for r in rows:
        if r.type in ("buy", "sell") and r.fees:
            if r.currency == "CNY":
                total_fees_cny += r.fees
            else:
                conv = convert_to_cny(r.fees, r.currency, db)
                if conv["valid"] and conv["rate"] is not None:
                    total_fees_cny += conv["value"]

    summary = {
        "total_cost_cny": round(total_cost_cny, 2),
        "total_value_cny": round(total_value_cny, 2),
        "total_profit_cny": round(total_value_cny - total_cost_cny, 2),
        "total_profit_pct": round((total_value_cny - total_cost_cny) / total_cost_cny * 100, 2) if total_cost_cny > 0 else 0,
        "daily_change_cny": round(daily_pnl_cny, 2),
        "daily_change_pct": round(daily_pnl_cny / total_value_cny * 100, 2) if total_value_cny > 0 else 0,
        "holdings_count": len(result),
        "total_fees_cny": round(total_fees_cny, 2),
    }

    return {"holdings": result, "summary": summary}


def _load_performance_cache(db: Session):
    rows = db.query(PerformanceSnapshot).order_by(PerformanceSnapshot.date).all()
    if not rows:
        return {}
    return {
        "dates": [r.date for r in rows],
        "portfolio": [r.portfolio_pct for r in rows],
        "qqq": [r.qqq_pct for r in rows],
        "spy": [r.spy_pct for r in rows],
    }


def _save_performance_cache(data: dict, db: Session):
    dates = data.get("dates", [])
    pf = data.get("portfolio", [])
    qq = data.get("qqq", [])
    sp = data.get("spy", [])
    for i, d in enumerate(dates):
        existing = db.query(PerformanceSnapshot).filter(PerformanceSnapshot.date == d).first()
        if existing:
            existing.portfolio_pct = pf[i] if i < len(pf) else None
            existing.qqq_pct = qq[i] if i < len(qq) else None
            existing.spy_pct = sp[i] if i < len(sp) else None
            existing.updated_at = _now()
        else:
            db.add(PerformanceSnapshot(
                date=d,
                portfolio_pct=pf[i] if i < len(pf) else None,
                qqq_pct=qq[i] if i < len(qq) else None,
                spy_pct=sp[i] if i < len(sp) else None,
                updated_at=_now(),
            ))
    db.commit()


@app.get("/api/portfolio/performance")
def api_portfolio_performance(refresh: bool = False, db: Session = Depends(get_db)):
    """Return daily portfolio value + QQQ/SPY benchmarks since first transaction."""
    from datetime import datetime as _dt, timedelta as _td
    import yfinance as _yf
    import pandas as _pd
    import math as _math

    # Use cache when available and not forcing refresh
    if not refresh:
        cached = _load_performance_cache(db)
        if cached.get("dates"):
            return {"dates": cached["dates"], "portfolio": cached["portfolio"],
                    "qqq": cached.get("qqq", []), "spy": cached.get("spy", [])}

    rows = db.query(InvestmentRecord).order_by(InvestmentRecord.date).all()
    if not rows:
        return {"dates": [], "portfolio": [], "qqq": [], "spy": []}

    # Find date range
    start_date = min(r.date for r in rows)
    end_date = _dt.now().strftime("%Y-%m-%d")

    # Get closing prices for all assets + QQQ/SPY
    tickers = set(r.asset_name for r in rows if r.type in ("buy", "sell"))
    tickers.update(["QQQ", "SPY"])
    closes = {}
    for tkr in tickers:
        try:
            tk = _yf.Ticker(tkr)
            hist = tk.history(start=start_date, end=end_date, auto_adjust=True)
            if not hist.empty:
                closes[tkr] = hist["Close"]
        except Exception:
            pass

    if not closes:
        # Return cached data as fallback
        cached = _load_performance_cache(db)
        if cached.get("dates"):
            return {"dates": cached["dates"], "portfolio": cached["portfolio"],
                    "qqq": cached.get("qqq", []), "spy": cached.get("spy", [])}
        return {"dates": [], "portfolio": [], "qqq": [], "spy": []}

    # Build combined price index
    df = _pd.DataFrame(closes).ffill()
    dates = [d.strftime("%Y-%m-%d") for d in df.index]

    # Compute daily portfolio value
    port_val = [0.0] * len(df)
    # For each buy/sell, accumulate shares at each point
    ticker_shares = {t: [0.0] * len(df) for t in tickers if t not in ("QQQ", "SPY")}
    for r in rows:
        if r.type not in ("buy", "sell") or r.asset_name not in ticker_shares:
            continue
        tkr = r.asset_name
        qty = r.quantity or 0
        date_str = r.date
        try:
            idx = dates.index(date_str)
        except ValueError:
            continue
        if r.type == "buy":
            for i in range(idx, len(df)):
                ticker_shares[tkr][i] += qty
        elif r.type == "sell":
            for i in range(idx, len(df)):
                ticker_shares[tkr][i] -= qty

    for i in range(len(df)):
        v = 0.0
        for tkr, sh in ticker_shares.items():
            if tkr in closes and i < len(closes[tkr]):
                px = float(closes[tkr].iloc[i])
                if not _math.isnan(px):
                    v += sh[i] * px
        port_val[i] = round(v, 2)

    # Cumulative return accounting for cash flows (cost basis)
    cum_cost = 0.0
    costs = [0.0] * len(df)
    for r in rows:
        if r.type == "buy":
            cum_cost += r.total_amount + (r.fees or 0)
        elif r.type == "sell":
            cum_cost -= r.total_amount - (r.fees or 0)
        try:
            idx = dates.index(r.date)
            costs[idx] = cum_cost
        except ValueError:
            pass
    # Forward-fill costs
    for i in range(1, len(costs)):
        if costs[i] == 0:
            costs[i] = costs[i-1]

    port_pct = [round((port_val[i] / costs[i] - 1) * 100, 2) if costs[i] > 0 else 0 for i in range(len(port_val))]
    qqq_pct = []
    spy_pct = []
    if "QQQ" in closes and len(closes["QQQ"]) > 0:
        qqq0 = float(closes["QQQ"].iloc[0])
        qqq_pct = [round((float(closes["QQQ"].iloc[i]) / qqq0 - 1) * 100, 2) for i in range(len(df))]
    if "SPY" in closes and len(closes["SPY"]) > 0:
        spy0 = float(closes["SPY"].iloc[0])
        spy_pct = [round((float(closes["SPY"].iloc[i]) / spy0 - 1) * 100, 2) for i in range(len(df))]

    result = {"dates": dates, "portfolio": port_pct, "qqq": qqq_pct, "spy": spy_pct}
    _save_performance_cache(result, db)
    return result


@app.get("/api/resolve-type/{code}")
def api_resolve_type(code: str, db: Session = Depends(get_db)):
    """Auto-detect asset type + name. Saves name to market_prices for future use."""
    import httpx as _httpx
    result = {"type": "stock", "currency": "USD", "name": None}

    # Chinese 6-digit code: query East Money
    if code.isdigit() and len(code) == 6:
        try:
            resp = _httpx.get(
                "https://searchapi.eastmoney.com/api/suggest/get",
                params={"input": code, "type": 14, "token": "D43BF722C8E33BDC906FB84D85E326E8"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0
            )
            data = resp.json()
            if data.get("QuotationCodeTable"):
                r = data["QuotationCodeTable"]["Data"][0]
                sec_type = r.get("SecurityTypeName", "")
                name = r.get("Name", "")
                mkt = r.get("Market", "")
                type_map = {
                    "深A": "stock", "沪A": "stock", "深B": "stock", "沪B": "stock",
                    "创业板": "stock", "科创板": "stock",
                    "开放式基金": "fund", "基金": "fund", "ETF": "etf", "LOF": "fund",
                    "港股": "stock", "美股": "stock",
                }
                asset_type = type_map.get(sec_type, "fund" if mkt == "0" else "stock")
                currency = "CNY"
                if mkt in ("116", "105", "106", "107"): currency = "USD"
                elif mkt == "155": currency = "HKD"
                result = {"type": asset_type, "currency": currency, "name": name}
        except Exception:
            result = {"type": "fund", "currency": "CNY", "name": None}
    else:
        # Letter ticker: query Tencent
        try:
            resp = _httpx.get(f"https://qt.gtimg.cn/q=us{code.upper()}",
                              headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0)
            text = resp.text
            import re
            m = re.search(r'="([^"]+)"', text)
            if m:
                fields = m.group(1).split("~")
                name = fields[1] if len(fields) > 1 and fields[1] else code.upper()
                price = None
                try: price = float(fields[3]) if fields[3] else None
                except: pass
                sec_type = fields[55] if len(fields) > 55 else ""
                is_etf = "ETF" in sec_type or "ETF" in name
                result = {"type": "etf" if is_etf else "stock", "currency": "USD", "name": name, "price": price}
        except Exception:
            pass

    # Save name to market_prices for future lookups
    if result.get("name"):
        existing = db.query(MarketPrice).filter(MarketPrice.ticker == code).first()
        if existing:
            if not existing.name or existing.name == code:
                existing.name = result["name"]
                db.commit()
        else:
            from datetime import date
            db.add(MarketPrice(ticker=code, date=date.today().isoformat(),
                               close_price=0, name=result["name"], source="resolver"))
            db.commit()

    return result


def _resolve_secid(symbol: str, asset_type: str = "stock") -> str | None:
    """Map a user-facing ticker to an API target.

    Returns:
      East Money secid  → "market.ticker"  (e.g. "105.QQQ", "1.600519")
      Fund code         → "fund:CODE"       (e.g. "fund:016452")
      None              → unrecognised
    """
    symbol = symbol.strip().upper()
    if not symbol:
        return None

    # Chinese OTC fund (mutual fund)
    if asset_type == "fund":
        return f"fund:{symbol}"

    # 6-digit numeric — could be A-share stock or fund
    if symbol.isdigit() and len(symbol) == 6:
        # Shanghai A-share: 60xxxx, 68xxxx, 5xxxxx
        if symbol.startswith(("60", "68", "5")):
            return f"1.{symbol}"
        # Shenzhen A-share: 00xxxx, 30xxxx
        if symbol.startswith(("00", "30")):
            return f"0.{symbol}"
        # Remaining 6-digit codes (016xxx, 050xxx etc.) → likely fund
        return f"fund:{symbol}"

    # Hong Kong: 1-5 digits → zero-pad to 5
    if symbol.isdigit() and 1 <= len(symbol) <= 5:
        return f"116.{symbol.zfill(5)}"

    # US / other alphabetic tickers → try NASDAQ first
    clean = symbol.replace(".O", "").replace(".N", "").replace(".", "").replace("-", "")
    if clean.isalpha():
        return f"105.{clean}"

    return None


def _fetch_crypto_price(symbol: str):
    """Fetch crypto price from CoinGecko free API. Returns (price_usd, name, change_pct)."""
    import httpx as _httpx

    cg_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
        "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin", "AVAX": "avalanche-2",
        "DOT": "polkadot", "MATIC": "matic-network", "POL": "polygon-ecosystem-token",
        "LINK": "chainlink", "UNI": "uniswap", "ATOM": "cosmos",
        "USDT": "tether", "USDC": "usd-coin",
    }
    cg_id = cg_map.get(symbol.strip().upper(), symbol.strip().lower())

    try:
        resp = _httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd", "include_24hr_change": "true"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if cg_id not in data:
            print(f"[portfolio] CoinGecko: {cg_id} not found")
            return None, None, None
        d = data[cg_id]
        price = d.get("usd")
        if price is None:
            return None, None, None
        change = d.get("usd_24h_change")
        change_pct = round(change, 2) if change is not None else None
        tag = f"{change_pct:+.2f}%" if change_pct is not None else "?"
        print(f"[portfolio] crypto:{cg_id} = ${price:.4f}  {tag}")
        return round(price, 4), cg_id.replace("-", " ").title(), change_pct
    except Exception as e:
        print(f"[portfolio] Crypto price fetch failed for {cg_id}: {e}")
        return None, None, None


def _fetch_fund_price(code: str):
    """Fetch Chinese OTC fund price from 天天基金 (fundgz). Returns (price, name, change_pct)."""
    import httpx as _httpx
    import re as _re

    try:
        resp = _httpx.get(
            f"https://fundgz.1234567.com.cn/js/{code}.js",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8.0,
        )
        resp.raise_for_status()
        # Response is JSONP:  jsonpgz({...})
        text = resp.text.strip()
        match = _re.search(r"jsonpgz\((\{.*\})\)", text, _re.DOTALL)
        if not match:
            return None, None, None
        data = __import__("json").loads(match.group(1))
        name = data.get("name", "")
        gsz = float(data.get("gsz", 0) or 0)          # estimated NAV or latest actual
        gszzl = float(data.get("gszzl", 0) or 0)       # change % (e.g. 0.45 = +0.45%)
        if gsz <= 0:
            return None, None, None
        print(f"[portfolio] fund:{code} ({name}) = {gsz:.4f}  {gszzl:+.2f}%")
        return round(gsz, 4), name, round(gszzl, 2)
    except Exception as e:
        print(f"[portfolio] Fund price fetch failed for {code}: {e}")
        return None, None, None


def _fetch_sina_price(symbol: str):
    """Fetch current price from Sina Finance (国内可访问). Returns (price, name, change_pct)."""
    import httpx as _httpx, re
    sid = f"gb_{symbol.lower()}"  # US stocks
    try:
        resp = _httpx.get(f"https://hq.sinajs.cn/list={sid}",
                          headers={"Referer": "https://finance.sina.com.cn"}, timeout=10.0)
        resp.raise_for_status()
        text = resp.text
        m = re.search(r'="([^"]+)"', text)
        if not m: return None, None, None
        fields = m.group(1).split(",")
        if len(fields) < 2 or not fields[1] or fields[1] == "0.0000": return None, None, None
        name = fields[0]
        price = float(fields[1])
        chg = round(float(fields[2]) / price * 100, 2) if len(fields) > 2 and fields[2] and float(fields[2]) != 0 else None
        print(f"[sina] {sid} ({name}) = {price:.2f}  {chg:+.2f}%" if chg else f"[sina] {sid} ({name}) = {price:.2f}")
        return price, name, chg
    except Exception as e:
        print(f"[sina] Failed for {sid}: {e}")
        return None, None, None


def _fetch_tencent_price(symbol: str):
    """Fetch price from Tencent Finance (国内可访问，中文名称好). Returns (price, name, change_pct)."""
    import httpx as _httpx, re
    sid = f"us{symbol.upper()}"
    try:
        resp = _httpx.get(f"https://qt.gtimg.cn/q={sid}",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0)
        resp.raise_for_status()
        text = resp.text
        m = re.search(r'="([^"]+)"', text)
        if not m: return None, None, None
        fields = m.group(1).split("~")
        # Tencent format for US: ~name~code~price~... with many fields
        if len(fields) < 4: return None, None, None
        name = fields[1] if fields[1] else symbol.upper()
        try:
            price = float(fields[3])
        except (ValueError, IndexError):
            return None, None, None
        if price <= 0: return None, None, None
        chg = None
        try:
            if len(fields) > 32 and fields[32]:
                chg = round(float(fields[32]), 2)
        except (ValueError, IndexError):
            pass
        print(f"[tencent] {sid} ({name}) = {price:.2f}  {chg:+.2f}%" if chg else f"[tencent] {sid} ({name}) = {price:.2f}")
        return price, name, chg
    except Exception as e:
        print(f"[tencent] Failed for {sid}: {e}")
        return None, None, None


def _fetch_price(symbol: str, currency: str, db: Session, asset_type: str = "stock"):
    """Fetch current price. Returns (price, price_cny, name, change_pct).

    Routes to:
      - _fetch_crypto_price()  for crypto
      - _fetch_fund_price()    for Chinese OTC mutual funds
      - East Money push2 API   for stocks / ETFs / REITs (A-share / HK / US)
    Falls back from NASDAQ (105) → NYSE (106) for US tickers.
    Types with no live price (bond/option/future/forex/index/commodity/cash/other) return None.
    """
    import httpx as _httpx

    # ── Crypto path ──────────────────────────────────
    if asset_type == "crypto":
        price, name, change_pct = _fetch_crypto_price(symbol)
        if price is None:
            return None, None, None, None
        # Crypto price from CoinGecko is always USD
        if currency.upper() == "USD":
            cny_price = convert_to_cny(price, "USD", db)["value"]
        else:
            cny_price = price  # non-USD crypto is rare, treat as-is
        return price, round(cny_price, 2), name, change_pct

    # ── Types without live price ─────────────────────
    if asset_type in ("bond", "option", "future", "forex", "index", "commodity", "cash", "other"):
        print(f"[portfolio] {asset_type}:{symbol} — no live price API, manual only")
        return None, None, None, None

    # ── US stock path: Tencent first (better names), Sina fallback ──
    if asset_type in ("stock", "etf") and not symbol.isdigit():
        for fetcher in [_fetch_tencent_price, _fetch_sina_price]:
            price, name, chg = fetcher(symbol)
            if price is not None:
                cny_price = convert_to_cny(price, currency, db)["value"] if currency.upper() != "CNY" else price
                return round(price, 2), round(cny_price, 2), name, chg

    target = _resolve_secid(symbol, asset_type)
    if target is None:
        print(f"[portfolio] Could not resolve: {symbol}")
        return None, None, None, None

    # ── Fund path ──────────────────────────────────
    if target.startswith("fund:"):
        code = target[5:]
        price, name, change_pct = _fetch_fund_price(code)
        if price is None:
            return None, None, None, None
        cny_price = price  # Chinese funds are always CNY-denominated
        return price, cny_price, name, change_pct

    # ── Stock path ─────────────────────────────────
    markets_to_try = [target]
    if target.startswith("105."):
        markets_to_try.append(f"106.{target[4:]}")

    for sid in markets_to_try:
        try:
            resp = _httpx.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={
                    "secid": sid,
                    "fields": "f43,f57,f170",
                    "invt": "2",
                    "fltt": "1",
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("rc") != 0 or not data.get("data"):
                continue

            price_raw = data["data"].get("f43")
            if price_raw is None or price_raw == 0 or price_raw == "-":
                continue

            price_raw = float(price_raw)
            market = sid.split(".")[0]

            if market in ("0", "1"):
                price_raw = price_raw / 100     # A-share: fen → yuan
            else:
                price_raw = price_raw / 1000    # US / HK: price × 1000

            name = data["data"].get("f57", "")
            change_pct = data["data"].get("f170")      # may be None, int, or float
            if change_pct is not None and change_pct != "-":
                change_pct = round(float(change_pct) / 100, 2)
            else:
                change_pct = None

            print(f"[portfolio] {sid} ({name}) = {price_raw:.2f}  {change_pct:+.2f}%" if change_pct is not None else f"[portfolio] {sid} ({name}) = {price_raw:.2f}")

            if currency.upper() != "CNY":
                cny_price = convert_to_cny(price_raw, currency, db)["value"]
            else:
                cny_price = price_raw
            return round(price_raw, 2), round(cny_price, 2), name, change_pct

        except Exception as e:
            print(f"[portfolio] Price fetch failed for {sid}: {e}")
            continue

    # ── Fallback: Sina/Tencent for Chinese A-shares ──
    if symbol.isdigit() and len(symbol) == 6:
        import httpx as _httpx, re
        exchange = "sz" if symbol.startswith(("0", "3")) else "sh"
        txt_sid = f"{exchange}{symbol}"
        for base_url in ["https://qt.gtimg.cn/q=", "https://hq.sinajs.cn/list="]:
            try:
                resp = _httpx.get(f"{base_url}{txt_sid}",
                                  headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
                                  timeout=10.0)
                resp.raise_for_status()
                m = re.search(r'="([^"]+)"', resp.text)
                if m:
                    fields = m.group(1).split(",")
                    name = fields[0]
                    # Sina: fields=[name, open, prev_close, price, high, low, ...]
                    # Tencent: fields=[market, name, code, price, prev_close, open, ...]
                    if len(fields) > 3 and fields[3]:
                        px = None
                        prev_close = None
                        if "qt.gtimg" in base_url:
                            # Tencent: field 3=price, field 4=prev_close
                            try: px = float(fields[3])
                            except: pass
                            try: prev_close = float(fields[4])
                            except: pass
                        else:
                            # Sina: field 3=price, field 2=prev_close
                            try: px = float(fields[3])
                            except: pass
                            try: prev_close = float(fields[2])
                            except: pass
                        if px and px > 0:
                            chg = None
                            if prev_close and prev_close > 0:
                                chg = round((px - prev_close) / prev_close * 100, 2)
                            print(f"[A-share] {txt_sid} ({name}) = {px:.2f}  {chg:+.2f}%" if chg else f"[A-share] {txt_sid} ({name}) = {px:.2f}")
                            cny_price = px
                            return round(px, 2), round(cny_price, 2), name, chg
            except Exception as e:
                pass

    # ── Fallback: yfinance for non-Chinese tickers ──
    if asset_type in ("stock", "etf") and not symbol.isdigit():
        try:
            import yfinance as _yf
            from datetime import datetime as _dt, timedelta as _td
            tk = _yf.Ticker(symbol)
            end_dt = _dt.now()
            start_dt = end_dt - _td(days=5)
            hist = tk.history(start=start_dt, end=end_dt, auto_adjust=True)
            if not hist.empty:
                px = float(hist["Close"].iloc[-1])
                try:
                    info = _yf.Ticker(symbol).info
                    name = info.get('longName') or info.get('shortName') or symbol
                except Exception:
                    name = symbol
                prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else px
                change = (px - prev) / prev if prev > 0 else 0
                if currency.upper() != "CNY":
                    cny_price = convert_to_cny(px, currency, db)["value"]
                else:
                    cny_price = px
                print(f"[portfolio] yfinance:{symbol} = {px:.2f}  {change*100:+.2f}%")
                return round(px, 2), round(cny_price, 2), name, round(change * 100, 2)
        except Exception as e:
            print(f"[portfolio] yfinance fallback failed for {symbol}: {e}")

    return None, None, None, None


# ── Exchange Rate API ────────────────────────────────────────

@app.get("/api/exchange-rates")
def api_exchange_rates(db: Session = Depends(get_db)):
    rows = db.query(ExchangeRate).all()
    return [{
        "from_currency": r.from_currency,
        "to_currency": r.to_currency,
        "rate": r.rate,
        "source": r.source,
        "fetched_at": r.fetched_at,
    } for r in rows]


@app.post("/api/exchange-rates/refresh")
def api_exchange_rates_refresh(db: Session = Depends(get_db)):
    refresh_all_rates(db)
    return {"ok": True, "rates": api_exchange_rates(db)}


# ── Stats API ────────────────────────────────────────────────

@app.get("/api/stats/total")
def api_stats_total(year: int, month: int, db: Session = Depends(get_db)):
    accounts = db.query(Account).filter(Account.is_active == 1).all()
    if not accounts:
        return {"total_cny": 0, "prev_total_cny": 0, "income_cny": 0, "spending_cny": 0}

    cur_data = api_balances(year, month, db)
    total_cny = cur_data["total_cny"]

    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    prev_data = api_balances(prev_year, prev_month, db)
    prev_total_cny = prev_data["total_cny"]

    incomes = db.query(IncomeRecord).filter(
        IncomeRecord.year == year, IncomeRecord.month == month
    ).all()
    income_cny = sum(r.amount for r in incomes)

    spending_cny = round(prev_total_cny + income_cny - total_cny, 2)

    return {
        "year": year, "month": month,
        "total_cny": total_cny,
        "prev_total_cny": prev_total_cny,
        "income_cny": income_cny,
        "spending_cny": spending_cny,
    }


@app.get("/api/stats/trend")
def api_stats_trend(months: int = 12, db: Session = Depends(get_db)):
    now = datetime.now()
    result = []
    for offset in range(months - 1, -1, -1):
        m = now.month - offset
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        stats = api_stats_total(y, m, db)
        result.append(stats)
    return result


@app.get("/api/stats/expense-breakdown")
def api_expense_breakdown(year: int, month: int, db: Session = Depends(get_db)):
    prefix = f"{year:04d}-{month:02d}"
    rows = db.query(ExpenseRecord).filter(
        ExpenseRecord.datetime.like(f"{prefix}%")
    ).all()
    breakdown = {}
    for r in rows:
        breakdown[r.category] = breakdown.get(r.category, 0) + r.amount
    return [
        {"category": k, "amount": round(v, 2)}
        for k, v in sorted(breakdown.items(), key=lambda x: -x[1])
    ]


@app.get("/api/stats/investment")
def api_stats_investment(db: Session = Depends(get_db)):
    rows = db.query(InvestmentRecord).all()

    def _to_cny(amount, currency):
        if currency.upper() == "CNY":
            return amount
        conv = convert_to_cny(amount, currency, db)
        return conv["value"] if conv["valid"] and conv["rate"] is not None else 0

    total_invested = sum(_to_cny(r.total_amount, r.currency) for r in rows if r.type in ("buy", "dca_buy"))
    total_deposit = sum(_to_cny(r.total_amount, r.currency) for r in rows if r.type == "deposit")
    total_withdraw = sum(_to_cny(r.total_amount, r.currency) for r in rows if r.type == "withdraw")
    total_sold = sum(_to_cny(r.total_amount, r.currency) for r in rows if r.type == "sell")
    total_dividend = sum(_to_cny(r.total_amount, r.currency) for r in rows if r.type == "dividend")

    inv_accounts = db.query(Account).filter(
        Account.is_active == 1, Account.type == "investment"
    ).all()
    now_dt = datetime.now()
    inv_balances = []
    for a in inv_accounts:
        mb = db.query(MonthlyBalance).filter(
            MonthlyBalance.account_id == a.id,
            MonthlyBalance.year == now_dt.year,
            MonthlyBalance.month == now_dt.month,
        ).first()
        bal = mb.balance if mb else 0.0
        conv = convert_to_cny(bal, a.currency, db)
        inv_balances.append({
            "account_name": a.name,
            "currency": a.currency,
            "balance": bal,
            "balance_cny": round(conv["value"], 2) if conv["valid"] else None,
            "valid_currency": conv["valid"],
        })

    return {
        "total_invested_cny": round(total_invested + total_deposit, 2),
        "total_withdraw_cny": round(total_withdraw, 2),
        "total_sold_cny": round(total_sold, 2),
        "total_dividend_cny": round(total_dividend, 2),
        "accounts": inv_balances,
    }


# ── Transfer API ────────────────────────────────────────

@app.post("/api/transfers")
def api_transfer(data: dict, db: Session = Depends(get_db)):
    """Transfer money between two accounts. Creates corresponding investment records."""
    from_id = data.get("from_account_id")
    to_id = data.get("to_account_id")
    amount = float(data.get("amount", 0))
    currency = data.get("currency", "CNY")
    date_str = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    notes = data.get("notes", "")

    if not from_id or not to_id:
        raise HTTPException(400, "请选择转出和转入账户")
    if amount <= 0:
        raise HTTPException(400, "金额必须大于0")

    from_acc = db.query(Account).filter(Account.id == from_id).first()
    to_acc = db.query(Account).filter(Account.id == to_id).first()
    if not from_acc or not to_acc:
        raise HTTPException(400, "账户不存在")

    # Create withdraw from source
    db.add(InvestmentRecord(
        date=date_str, type="withdraw", asset_name=f"转账至 {to_acc.name}",
        asset_type="cash", total_amount=amount, currency=currency,
        account_id=from_id, notes=notes,
    ))
    # Create deposit to target
    db.add(InvestmentRecord(
        date=date_str, type="deposit", asset_name=f"来自 {from_acc.name} 转账",
        asset_type="cash", total_amount=amount, currency=currency,
        account_id=to_id, notes=notes,
    ))
    db.commit()
    return {"ok": True, "msg": f"已从「{from_acc.name}」转账 {amount} {currency} 到「{to_acc.name}」"}


# ── Backtest API ────────────────────────────────────────

@app.post("/api/backtest/run")
def api_backtest_run(data: dict):
    """Run configurable drawdown-ladder backtest."""
    from backtest import run_backtest

    result = run_backtest(data)
    return result


@app.post("/api/backtest/report")
def api_backtest_report(data: dict):
    """Generate backtest PDF report."""
    from backtest import run_backtest
    from fastapi.responses import Response

    result = run_backtest(data)
    # import gen_pdf from output/
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "gen_pdf",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "gen_pdf.py")
    )
    gen_pdf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen_pdf)

    html = gen_pdf.build_html(result)
    pdf_bytes = gen_pdf.html_to_pdf(html)
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=backtest_report.pdf"})


# ── Backup / Restore API ──────────────────────────────────

def _derive_key(password: str, salt: bytes):
    """Derive AES-256 key from password using PBKDF2."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600000)
    return kdf.derive(password.encode("utf-8"))


def _encrypt_data(plaintext: str, password: str) -> bytes:
    """Encrypt plaintext with AES-256-GCM. Returns salt + iv + tag + ciphertext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(16)
    key = _derive_key(password, salt)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return salt + nonce + ct


def _decrypt_data(data: bytes, password: str) -> str:
    """Decrypt AES-256-GCM ciphertext. Expects salt(16) + nonce(12) + ciphertext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce, ct = data[:16], data[16:28], data[28:]
    key = _derive_key(password, salt)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


@app.post("/api/backup/export")
def api_backup_export(data: dict, db: Session = Depends(get_db)):
    """Export all user data as encrypted JSON."""
    from fastapi.responses import Response

    password = data.get("password", "")
    if len(password) < 4:
        raise HTTPException(400, "密码至少4位")

    def row_to_dict(row):
        d = {}
        for col in row.__table__.columns:
            v = getattr(row, col.name)
            if isinstance(v, datetime):
                v = v.strftime("%Y-%m-%d %H:%M:%S")
            d[col.name] = v
        return d

    payload = {
        "version": 1,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "accounts": [row_to_dict(r) for r in db.query(Account).all()],
        "monthly_balances": [row_to_dict(r) for r in db.query(MonthlyBalance).all()],
        "income_records": [row_to_dict(r) for r in db.query(IncomeRecord).all()],
        "expense_records": [row_to_dict(r) for r in db.query(ExpenseRecord).all()],
        "expense_categories": [row_to_dict(r) for r in db.query(ExpenseCategory).all()],
        "recurring_expenses": [row_to_dict(r) for r in db.query(RecurringExpense).all()],
        "investment_records": [row_to_dict(r) for r in db.query(InvestmentRecord).all()],
        "exchange_rates": [row_to_dict(r) for r in db.query(ExchangeRate).all()],
        "dca_plans": [row_to_dict(r) for r in db.query(DcaPlan).all()],
    }
    encrypted = _encrypt_data(json.dumps(payload, ensure_ascii=False), password)
    return Response(content=encrypted, media_type="application/octet-stream",
                    headers={"Content-Disposition": "attachment; filename=ledger_backup.enc"})


@app.post("/api/backup/import")
def api_backup_import(request: dict, db: Session = Depends(get_db)):
    """Import data from an encrypted backup. Merges: updates existing, inserts new."""
    from fastapi import UploadFile, File, Form
    raise HTTPException(400, "Use multipart form: password + file")


@app.post("/api/backup/import/file")
async def api_backup_import_file(password: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Import data from encrypted backup file."""
    raw = await file.read()
    try:
        plain = _decrypt_data(raw, password)
    except Exception:
        raise HTTPException(400, "密码错误或文件损坏")

    data = json.loads(plain)
    imported = {}
    errors = []

    def _import_rows(key, Model, unique_cols, fk_clear):
        rows = data.get(key, [])
        count = 0
        cols = [c.name for c in Model.__table__.columns]
        for r in rows:
            existing = None
            filters = []
            for uc in unique_cols:
                if r.get(uc) is not None:
                    filters.append(getattr(Model, uc) == r[uc])
            if filters:
                existing = db.query(Model).filter(*filters).first()
            elif r.get("id"):
                existing = db.query(Model).filter(Model.id == r["id"]).first()
            for fk in fk_clear:
                r.pop(fk, None)
            row_id = r.pop("id", None)
            try:
                if existing:
                    for k, v in r.items():
                        if k in cols and k != "id":
                            setattr(existing, k, v)
                else:
                    obj = Model(**{k: v for k, v in r.items() if k in cols})
                    if row_id:
                        obj.id = row_id
                    db.add(obj)
                count += 1
            except Exception as e:
                errors.append(f"{key}: {e}")
        db.commit()
        imported[key] = count

    _import_rows("accounts", Account, ["name"], ["parent_id"])
    _import_rows("monthly_balances", MonthlyBalance, ["account_id", "year", "month"], [])
    _import_rows("income_records", IncomeRecord, [], [])
    _import_rows("expense_records", ExpenseRecord, [], [])
    _import_rows("expense_categories", ExpenseCategory, ["name"], [])
    _import_rows("recurring_expenses", RecurringExpense, [], [])
    _import_rows("investment_records", InvestmentRecord, [], [])
    _import_rows("exchange_rates", ExchangeRate, ["from_currency", "to_currency"], [])
    _import_rows("dca_plans", DcaPlan, [], [])

    return {"ok": True, "imported": imported, "errors": errors}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
