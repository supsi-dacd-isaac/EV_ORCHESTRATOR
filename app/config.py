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

# Master key used to encrypt/decrypt per-pilot secrets (pilot_secret.ciphertext).
# Must be a urlsafe base64 32-byte Fernet key. Lives ONLY in the environment
# (.env.local / platform secret store), never in git or the database. If it is
# lost or changed, previously stored secrets can no longer be decrypted.
# Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
EV_SECRETS_KEY = os.getenv("EV_SECRETS_KEY", "")
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

# ---------------------------------------------------------------------------
# Celery / Redis
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
REDBEAT_REDIS_URL = os.getenv("REDBEAT_REDIS_URL", "redis://localhost:6379/0")
REDBEAT_LOCK_TIMEOUT = int(os.getenv("REDBEAT_LOCK_TIMEOUT", "300"))
CELERY_VISIBILITY_TIMEOUT = int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "43200"))
CELERY_PURGE_ON_START = _get_bool_env("CELERY_PURGE_ON_START", default=False)

# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------
FORECASTERS_ARTIFACTS_DIR = os.getenv("FORECASTERS_ARTIFACTS_DIR", "")
FORECAST_JOBS_MANIFEST = os.getenv("FORECAST_JOBS_MANIFEST", "")
FORECAST_PRUNE_ORPHAN_REDBEAT = _get_bool_env("FORECAST_PRUNE_ORPHAN_REDBEAT", default=False)
_max_age_raw = os.getenv("FORECAST_ARTIFACT_MAX_AGE_DAYS", "")
FORECAST_ARTIFACT_MAX_AGE_DAYS: int = int(_max_age_raw) if _max_age_raw.strip() else 0

# ---------------------------------------------------------------------------
# CSV session import
# ---------------------------------------------------------------------------
# Maximum upload size in bytes (~300 rows at ~170 bytes/row)
CSV_IMPORT_MAX_BYTES = int(os.getenv("CSV_IMPORT_MAX_BYTES", str(52_428)))
# Maximum number of rows processed per import call
CSV_IMPORT_MAX_ROWS = int(os.getenv("CSV_IMPORT_MAX_ROWS", "200"))
