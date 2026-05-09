"""Personal ledger web app — FastAPI entry point."""
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from database import engine, get_db, init_db
from models import (
    Base, PRESET_CATEGORIES,
    Account, MonthlyBalance, IncomeRecord,
    ExpenseRecord, ExpenseCategory, RecurringExpense,
    InvestmentRecord, ExchangeRate, DcaPlan
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

app = FastAPI(title="个人账本")

app.mount("/static", StaticFiles(directory="static"), name="static")
jinja_env = Environment(loader=FileSystemLoader("templates"), autoescape=True)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def seed_categories(db: Session):
    existing = db.query(ExpenseCategory).count()
    if existing == 0:
        for i, name in enumerate(PRESET_CATEGORIES):
            db.add(ExpenseCategory(name=name, sort_order=i, is_preset=1))
        db.commit()


@app.on_event("startup")
def on_startup():
    init_db()
    db = next(get_db())
    try:
        seed_categories(db)
        refresh_all_rates(db)
    finally:
        db.close()


# ── Page routes ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def page_index():
    tpl = jinja_env.get_template("index.html")
    return HTMLResponse(tpl.render(nav="index"))


@app.get("/expenses", response_class=HTMLResponse)
def page_expenses():
    tpl = jinja_env.get_template("expenses.html")
    return HTMLResponse(tpl.render(nav="expenses"))


@app.get("/investment", response_class=HTMLResponse)
def page_investment():
    tpl = jinja_env.get_template("investment.html")
    return HTMLResponse(tpl.render(nav="investment"))


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
    accounts = db.query(Account).filter(Account.parent_id == None).order_by(Account.sort_order).all()
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

    if len(currencies) == 1:
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


# ── Monthly Balance API ──────────────────────────────────────

@app.get("/api/balances")
def api_balances(year: int, month: int, db: Session = Depends(get_db)):
    # Only top-level accounts (not sub-accounts)
    accounts = db.query(Account).filter(
        Account.is_active == 1, Account.parent_id == None
    ).order_by(Account.sort_order).all()

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
            balance = cur.balance if cur else prev_balances.get(a.id, 0.0)
            conv = convert_to_cny(balance, a.currency, db)
            if conv["valid"] and conv["rate"] is not None:
                total_cny += conv["value"]
            result.append({
                "account_id": a.id, "account_name": a.name,
                "type": a.type, "currency": a.currency,
                "balance": balance,
                "balance_cny": round(conv["value"], 2) if conv["valid"] else None,
                "rate": conv["rate"], "valid_currency": conv["valid"],
                "prev_balance": prev_balances.get(a.id),
                "has_record": cur is not None,
                "multi": False,
            })
    return {"items": result, "total_cny": round(total_cny, 2), "year": year, "month": month}


@app.put("/api/balances")
def api_balances_save(data: MonthlyBalanceSave, db: Session = Depends(get_db)):
    for entry in data.balances:
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
        "next_date": r.next_date, "is_active": r.is_active, "notes": r.notes,
    } for r in rows]


@app.post("/api/dca-plans")
def api_dca_create(data: dict, db: Session = Depends(get_db)):
    plan = DcaPlan(
        asset_name=data["asset_name"], asset_type=data.get("asset_type", "etf"),
        amount=data["amount"], fees=data.get("fees", 0),
        currency=data.get("currency", "CNY"), platform=data.get("platform", ""),
        account_id=data.get("account_id"), payment_account=data.get("payment_account"),
        frequency=data.get("frequency", "monthly"), next_date=data["next_date"],
        notes=data.get("notes", ""),
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
        db.delete(plan)
    else:
        plan.is_active = 0
        plan.updated_at = _now()
    db.commit()
    return {"ok": True, "hard": hard}


@app.post("/api/dca-plans/{plan_id}/execute")
def api_dca_execute(plan_id: int, price: float | None = None, note: str | None = None, db: Session = Depends(get_db)):
    """Manually execute a DCA plan — creates an investment record.

    price: if None, try to fetch current market price; if 0 or provided, use it.
    note: optional extra notes (e.g. balance warning).
    quantity = plan.amount / price  (fees don't buy shares)
    """
    plan = db.query(DcaPlan).filter(DcaPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "定投计划不存在")

    # Check payment account balance
    balance_ok = True
    if plan.payment_account:
        now_dt = datetime.now()
        pmt_bal = db.query(MonthlyBalance).filter(
            MonthlyBalance.account_id == plan.payment_account,
            MonthlyBalance.year == now_dt.year,
            MonthlyBalance.month == now_dt.month,
        ).first()
        if pmt_bal and pmt_bal.balance < plan.amount:
            balance_ok = False

    resolved_price = price
    if not resolved_price:
        p, _, _, _ = _fetch_price(plan.asset_name, plan.currency, db, plan.asset_type)
        if p:
            resolved_price = p

    qty = round(plan.amount / resolved_price, 4) if resolved_price else None

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
        fees=plan.fees or 0,
        total_amount=plan.amount,
        currency=plan.currency,
        platform=plan.platform,
        account_id=plan.account_id,
        notes=" | ".join(notes_parts),
    )
    db.add(inv)
    # Advance next_date
    from datetime import datetime as dt, timedelta
    nd = dt.strptime(plan.next_date, "%Y-%m-%d")
    if plan.frequency == "weekly":
        nd += timedelta(days=7)
    elif plan.frequency == "biweekly":
        nd += timedelta(days=14)
    else:
        # monthly: advance one month
        m = nd.month + 1
        y = nd.year
        if m > 12:
            m = 1
            y += 1
        nd = nd.replace(year=y, month=m)
    plan.next_date = nd.strftime("%Y-%m-%d")
    plan.updated_at = _now()
    db.commit()
    return {"ok": True, "investment_id": inv.id, "next_date": plan.next_date, "balance_ok": balance_ok}


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
    r = InvestmentRecord(**data.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return {"id": r.id, "asset_name": r.asset_name, "type": r.type}


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
            holdings[key]["total_cost"] += r.total_amount
        elif r.type == "sell":
            holdings[key]["quantity"] -= (r.quantity or 0)
            holdings[key]["total_cost"] -= r.total_amount

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
        if refresh:
            price, price_cny, name, change_pct = _fetch_price(
                h["asset_name"], h["currency"], db, h["asset_type"]
            )
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

    summary = {
        "total_cost_cny": round(total_cost_cny, 2),
        "total_value_cny": round(total_value_cny, 2),
        "total_profit_cny": round(total_value_cny - total_cost_cny, 2),
        "total_profit_pct": round((total_value_cny - total_cost_cny) / total_cost_cny * 100, 2) if total_cost_cny > 0 else 0,
        "daily_change_cny": round(daily_pnl_cny, 2),
        "daily_change_pct": round(daily_pnl_cny / total_value_cny * 100, 2) if total_value_cny > 0 else 0,
        "holdings_count": len(result),
    }

    return {"holdings": result, "summary": summary}


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


# ── Backtest API ────────────────────────────────────────

@app.post("/api/backtest/run")
def api_backtest_run(data: dict):
    """Run configurable drawdown-ladder backtest."""
    from backtest import run_backtest

    result = run_backtest(data)
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
