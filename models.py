from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from database import Base


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # bank, ewallet, credit, investment
    currency = Column(String, default="CNY")
    parent_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)  # parent account for multi-currency
    is_active = Column(Integer, default=1)
    sort_order = Column(Integer, default=0)
    notes = Column(String, default="")
    created_at = Column(String, default=_now)
    updated_at = Column(String, default=_now)

    monthly_balances = relationship("MonthlyBalance", back_populates="account", cascade="all, delete-orphan")
    incomes = relationship("IncomeRecord", back_populates="account")
    expenses = relationship("ExpenseRecord", back_populates="account")
    investments = relationship("InvestmentRecord", back_populates="account")
    sub_accounts = relationship("Account", backref="parent", remote_side=[id])


class MonthlyBalance(Base):
    __tablename__ = "monthly_balances"
    __table_args__ = (UniqueConstraint("account_id", "year", "month"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    balance = Column(Float, default=0.0)
    notes = Column(String, default="")
    created_at = Column(String, default=_now)
    updated_at = Column(String, default=_now)

    account = relationship("Account", back_populates="monthly_balances")


class IncomeRecord(Base):
    __tablename__ = "income_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    source = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    notes = Column(String, default="")
    created_at = Column(String, default=_now)
    updated_at = Column(String, default=_now)

    account = relationship("Account", back_populates="incomes")


class ExpenseRecord(Base):
    __tablename__ = "expense_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    datetime = Column(String, nullable=False)  # "2026-05-07 18:30"
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    category = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    description = Column(String, default="")
    recurring_id = Column(Integer, ForeignKey("recurring_expenses.id"), nullable=True)
    notes = Column(String, default="")
    created_at = Column(String, default=_now)
    updated_at = Column(String, default=_now)

    account = relationship("Account", back_populates="expenses")
    recurring = relationship("RecurringExpense", back_populates="expenses")


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    sort_order = Column(Integer, default=0)
    is_preset = Column(Integer, default=0)  # 1=built-in, 0=user-defined
    created_at = Column(String, default=_now)


class RecurringExpense(Base):
    __tablename__ = "recurring_expenses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    description = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    category = Column(String, nullable=False)
    start_year = Column(Integer, nullable=False)
    start_month = Column(Integer, nullable=False)
    end_year = Column(Integer, nullable=True)
    end_month = Column(Integer, nullable=True)
    payment_account = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    is_active = Column(Integer, default=1)
    notes = Column(String, default="")
    created_at = Column(String, default=_now)
    updated_at = Column(String, default=_now)

    expenses = relationship("ExpenseRecord", back_populates="recurring")
    payment_account_rel = relationship("Account", foreign_keys=[payment_account])


class InvestmentRecord(Base):
    __tablename__ = "investment_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String, nullable=False)
    type = Column(String, nullable=False)  # buy, sell, dividend, deposit, withdraw
    asset_name = Column(String, nullable=False)
    asset_type = Column(String, default="stock")  # stock/etf/fund/crypto/bond/option/future/forex/reit/index/commodity/cash/other
    quantity = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    fees = Column(Float, default=0.0)
    total_amount = Column(Float, nullable=False)
    currency = Column(String, default="CNY")
    platform = Column(String, default="")
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    notes = Column(String, default="")
    created_at = Column(String, default=_now)
    updated_at = Column(String, default=_now)

    account = relationship("Account", back_populates="investments")


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_currency = Column(String, nullable=False)
    to_currency = Column(String, default="CNY")
    rate = Column(Float, nullable=False)
    source = Column(String, default="frankfurter")
    fetched_at = Column(String, default=_now)


class DcaPlan(Base):
    __tablename__ = "dca_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_name = Column(String, nullable=False)
    asset_type = Column(String, default="etf")  # stock/etf/fund/crypto/bond/option/future/forex/reit/index/commodity/cash/other
    amount = Column(Float, nullable=False)
    fees = Column(Float, default=0.0)
    currency = Column(String, default="CNY")
    platform = Column(String, default="")
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)  # investment account
    payment_account = Column(Integer, ForeignKey("accounts.id"), nullable=True)  # funding source
    frequency = Column(String, default="monthly")  # weekly, biweekly, monthly
    next_date = Column(String, nullable=False)  # next execution date
    is_active = Column(Integer, default=1)
    notes = Column(String, default="")
    created_at = Column(String, default=_now)
    updated_at = Column(String, default=_now)


# Preset expense categories
PRESET_CATEGORIES = [
    "餐饮", "交通", "购物", "房租", "水电", "通讯", "娱乐",
    "医疗", "教育", "服饰", "日用品", "社交", "旅行", "数码",
    "宠物", "美容", "居家", "运动", "其他",
]
