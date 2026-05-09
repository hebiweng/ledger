"""Recurring expense auto-generation — creates expense records for each active recurring template."""
from sqlalchemy.orm import Session
from models import RecurringExpense, ExpenseRecord


def ensure_expenses_for_month(year: int, month: int, db: Session):
    """Ensure every active recurring expense has a corresponding expense_record for the given month."""
    all_recurring = db.query(RecurringExpense).filter(RecurringExpense.is_active == 1).all()
    created = 0
    prefix = f"{year:04d}-{month:02d}"

    for rec in all_recurring:
        # Check if this recurring item should fire in this month
        start = rec.start_year * 12 + rec.start_month
        current = year * 12 + month
        if current < start:
            continue
        if rec.end_year is not None and rec.end_month is not None:
            end = rec.end_year * 12 + rec.end_month
            if current > end:
                continue

        # Check if expense already exists for this recurring item this month
        existing = db.query(ExpenseRecord).filter(
            ExpenseRecord.recurring_id == rec.id,
            ExpenseRecord.datetime.like(f"{prefix}%"),
        ).first()
        if existing:
            continue

        # Create the expense record
        day = f"{year:04d}-{month:02d}-01"
        expense = ExpenseRecord(
            datetime=day,
            account_id=rec.payment_account,
            category=rec.category,
            amount=rec.amount,
            description=rec.description,
            recurring_id=rec.id,
        )
        db.add(expense)
        created += 1

    if created:
        db.commit()
        print(f"[recurring] Generated {created} expense records for {prefix}")
    return created
