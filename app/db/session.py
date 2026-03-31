import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _default_database_url() -> str:
    """
    When DATABASE_URL is unset:
    - Docker / Linux images often use user ``postgres`` + password ``postgre``.
    - macOS Homebrew / Postgres.app usually use your login name with no password in the URL.
    """
    user = os.getenv("PGUSER") or os.getenv("USER") or "postgres"
    password = os.getenv("PGPASSWORD")
    if password is None:
        password = "postgre" if user == "postgres" else ""

    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    db = os.getenv("PGDATABASE", "ev_orchestrator")

    if password:
        return f"postgresql://{user}:{password}@{host}:{port}/{db}"
    return f"postgresql://{user}@{host}:{port}/{db}"


DATABASE_URL = os.getenv("DATABASE_URL") or _default_database_url()

engine = create_engine(
    DATABASE_URL,
    echo=True
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False
)