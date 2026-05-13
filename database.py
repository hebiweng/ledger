from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

SQLALCHEMY_DATABASE_URL = "sqlite:///./ledger.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    # Migrations — add columns that might not exist yet
    import sqlite3
    conn = sqlite3.connect("ledger.db")
    cur = conn.execute("PRAGMA table_info(accounts)")
    cols = {row[1] for row in cur.fetchall()}
    if "parent_id" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN parent_id INTEGER REFERENCES accounts(id)")
    # dca_plans
    cur = conn.execute("PRAGMA table_info(dca_plans)")
    cols = {row[1] for row in cur.fetchall()}
    if "start_date" not in cols:
        conn.execute("ALTER TABLE dca_plans ADD COLUMN start_date VARCHAR")
    conn.commit()
    conn.close()
