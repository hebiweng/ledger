"""Exchange rate service — Frankfurter API with 24h caching.
Supports on-demand rates for any ISO 4217 currency.
"""
import httpx
from datetime import datetime
from sqlalchemy.orm import Session
from models import ExchangeRate

# ISO 4217 active currency codes (commonly used subset + full reference)
ISO4217 = {
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
    "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL",
    "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "CNY",
    "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP",
    "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD",
    "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR",
    "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF",
    "KRW", "KWD", "KZT", "LAK", "LBP", "LKR", "LYD", "MAD", "MDL", "MGA",
    "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN", "MYR",
    "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN",
    "PGK", "PHP", "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB", "RWF",
    "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP", "SLE", "SOS", "SRD",
    "SSP", "STN", "SYP", "SZL", "THB", "TJS", "TMT", "TND", "TOP", "TRY",
    "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VES", "VND",
    "VUV", "WST", "XAF", "XCD", "XOF", "XPF", "YER", "ZAR", "ZMW", "ZWL",
}

# Currencies to prefetch at startup (commonly used)
PREFETCH = ["USD", "HKD", "JPY", "EUR", "GBP"]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _needs_refresh(record: ExchangeRate) -> bool:
    if record.fetched_at is None:
        return True
    try:
        fetched = datetime.strptime(record.fetched_at, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - fetched).total_seconds() > 86400
    except Exception:
        return True


def validate_currency(code: str) -> bool:
    """Check if a currency code is a valid ISO 4217 code."""
    return code.upper() in ISO4217


def fetch_rate(from_currency: str, to_currency: str, db: Session) -> float | None:
    """Fetch a specific exchange rate from Frankfurter API. Returns None if unavailable."""
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return 1.0

    if not validate_currency(from_currency):
        print(f"[exchange] Invalid currency: {from_currency}")
        return None

    try:
        resp = httpx.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"from": from_currency, "to": to_currency},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = data["rates"][to_currency]
        _upsert_rate(db, from_currency, to_currency, rate)
        print(f"[exchange] Fetched: 1 {from_currency} = {rate} {to_currency}")
        return rate
    except Exception as e:
        print(f"[exchange] Fetch {from_currency}→{to_currency} failed: {e}")
        return None


def prefetch_rates(db: Session):
    """Fetch common currency rates at startup."""
    for cur in PREFETCH:
        fetch_rate(cur, "CNY", db)


def _upsert_rate(db: Session, from_cur: str, to_cur: str, rate: float):
    existing = db.query(ExchangeRate).filter(
        ExchangeRate.from_currency == from_cur,
        ExchangeRate.to_currency == to_cur,
    ).first()
    if existing:
        existing.rate = rate
        existing.fetched_at = _now()
    else:
        db.add(ExchangeRate(
            from_currency=from_cur, to_currency=to_cur,
            rate=rate, fetched_at=_now(),
        ))
    db.commit()


def get_rate(from_currency: str, to_currency: str, db: Session) -> float | None:
    """Get exchange rate. Returns None if currency is invalid or rate unavailable.
    1.0 for same currency. Uses cache if fresh, otherwise fetches live.
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return 1.0

    if not validate_currency(from_currency) or not validate_currency(to_currency):
        return None

    record = db.query(ExchangeRate).filter(
        ExchangeRate.from_currency == from_currency,
        ExchangeRate.to_currency == to_currency,
    ).first()

    if record and not _needs_refresh(record):
        return record.rate

    return fetch_rate(from_currency, to_currency, db)


def convert_to_cny(amount: float, currency: str, db: Session) -> dict:
    """Convert amount to CNY. Returns {"value": float, "rate": float|None, "valid": bool}.
    - valid=False: unrecognized currency code
    - rate=None: recognized but rate fetch failed
    """
    currency = currency.upper()
    if currency == "CNY":
        return {"value": round(amount, 2), "rate": 1.0, "valid": True}

    if not validate_currency(currency):
        return {"value": 0, "rate": None, "valid": False}

    rate = get_rate(currency, "CNY", db)
    if rate is None:
        return {"value": 0, "rate": None, "valid": True}

    return {"value": round(amount * rate, 2), "rate": rate, "valid": True}


def refresh_all_rates(db: Session):
    """Force refresh all exchange rates."""
    prefetch_rates(db)
