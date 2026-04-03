from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


APP_ENV = os.getenv("APP_ENV", "development")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgre@localhost:5432/ev_orchestrator",
)
DB_ECHO = _get_bool_env("DB_ECHO", default=False)

AUTH_SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "change-me-in-production")
AUTH_ALGORITHM = os.getenv("AUTH_ALGORITHM", "HS256")
AUTH_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("AUTH_ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
AUTH_PBKDF2_ITERATIONS = int(os.getenv("AUTH_PBKDF2_ITERATIONS", "390000"))

# Initial admin seed (only used when the owners table is empty)
ADMIN_INIT_USER = os.getenv("ADMIN_INIT_USER", "supsi_admin")
ADMIN_INIT_PASSWORD = os.getenv("ADMIN_INIT_PASSWORD", "")
ADMIN_INIT_COMPANY = os.getenv("ADMIN_INIT_COMPANY", "supsi")

DEFAULT_POLICY_FILENAME = "policy_20251204_120755_e4999_Actor.pth"

ACTOR_MODEL_PATH = Path(
    os.getenv(
        "ACTOR_MODEL_PATH",
        BASE_DIR / "models" / "actor" / DEFAULT_POLICY_FILENAME,
    )
)