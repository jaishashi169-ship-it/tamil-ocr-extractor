import os
import tempfile
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import get_settings

settings = get_settings()

sync_db_url = settings.SYNC_DATABASE_URL
if sync_db_url.startswith("sqlite:///./") and not os.access(".", os.W_OK):
    sqlite_name = Path(sync_db_url.replace("sqlite:///./", "", 1)).name
    sync_db_url = f"sqlite:///{(Path(tempfile.gettempdir()) / sqlite_name).as_posix()}"

connect_args = {"check_same_thread": False} if sync_db_url.startswith("sqlite") else {}

engine = create_engine(
    sync_db_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
