import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = str(os.getenv("DATABASE_URL", "")).strip()

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set. Docker-only mode requires PostgreSQL via docker compose.")

db_url = make_url(DATABASE_URL)
if not str(db_url.drivername).startswith("postgresql"):
    raise RuntimeError("Only PostgreSQL is supported in Docker-only mode. Configure PostgreSQL DATABASE_URL.")
if str(db_url.host or "").strip().lower() != "db":
    raise RuntimeError("DATABASE_URL host must be 'db' (Compose PostgreSQL service).")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
