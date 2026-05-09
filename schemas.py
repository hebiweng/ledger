from pydantic import BaseModel
from typing import Optional


class AccountCreate(BaseModel):
    name: str
    type: str
    currency: str = "CNY"
    currencies: Optional[list[str]] = None  # multi-currency mode
    is_active: int = 1
    sort_order: int = 0
    notes: str = ""


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    currency: Optional[str] = None
    is_active: Optional[int] = None
    sort_order: Optional[int] = None
    notes: Optional[str] = None


class MonthlyBalanceEntry(BaseModel):
    account_id: int
    balance: float


class MonthlyBalanceSave(BaseModel):
    year: int
    month: int
    balances: list[MonthlyBalanceEntry]


class IncomeCreate(BaseModel):
    year: int
    month: int
    source: str
    amount: float
    account_id: Optional[int] = None
    notes: str = ""


class IncomeUpdate(BaseModel):
    year: Optional[int] = None
    month: Optional[int] = None
    source: Optional[str] = None
    amount: Optional[float] = None
    account_id: Optional[int] = None
    notes: Optional[str] = None


class ExpenseCreate(BaseModel):
    datetime: str
    account_id: Optional[int] = None
    category: str
    amount: float
    description: str = ""
    recurring_id: Optional[int] = None
    notes: str = ""


class ExpenseUpdate(BaseModel):
    datetime: Optional[str] = None
    account_id: Optional[int] = None
    category: Optional[str] = None
    amount: Optional[float] = None
    description: Optional[str] = None
    notes: Optional[str] = None


class CategoryCreate(BaseModel):
    name: str


class RecurringCreate(BaseModel):
    description: str
    amount: float
    category: str
    start_year: int
    start_month: int
    end_year: Optional[int] = None
    end_month: Optional[int] = None
    payment_account: Optional[int] = None
    is_active: int = 1
    notes: str = ""


class RecurringUpdate(BaseModel):
    description: Optional[str] = None
    amount: Optional[float] = None
    category: Optional[str] = None
    start_year: Optional[int] = None
    start_month: Optional[int] = None
    end_year: Optional[int] = None
    end_month: Optional[int] = None
    payment_account: Optional[int] = None
    is_active: Optional[int] = None
    notes: Optional[str] = None


class InvestmentCreate(BaseModel):
    date: str
    type: str
    asset_name: str
    asset_type: str = "stock"
    quantity: Optional[float] = None
    price: Optional[float] = None
    fees: float = 0.0
    total_amount: float
    currency: str = "CNY"
    platform: str = ""
    account_id: Optional[int] = None
    notes: str = ""


class InvestmentUpdate(BaseModel):
    date: Optional[str] = None
    type: Optional[str] = None
    asset_name: Optional[str] = None
    asset_type: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    fees: Optional[float] = None
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    platform: Optional[str] = None
    account_id: Optional[int] = None
    notes: Optional[str] = None
