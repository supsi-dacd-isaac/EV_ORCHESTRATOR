from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
filename = "policy_20251204_120755_e4999_Actor.pth"

ACTOR_MODEL_PATH = Path(
    os.getenv(
        "ACTOR_MODEL_PATH",
        BASE_DIR / "models" / "actor" / filename,
    )
)