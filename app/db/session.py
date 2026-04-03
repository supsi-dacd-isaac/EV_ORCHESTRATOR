import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import DATABASE_URL, DB_ECHO

engine = create_engine(
    DATABASE_URL,
    echo=DB_ECHO
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False
)