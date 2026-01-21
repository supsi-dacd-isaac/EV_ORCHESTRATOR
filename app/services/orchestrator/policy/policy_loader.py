from functools import lru_cache
from pathlib import Path

from .policy import Policy
from app.config import ACTOR_MODEL_PATH


@lru_cache()
def get_policy() -> Policy:
    model_path = Path(ACTOR_MODEL_PATH)

    # assert model_path.exists(), (
    #     f"Actor model not found: {model_path.resolve()}"
    # )
    if not model_path.exists():
        raise RuntimeError("Model not found")

    return Policy(
        model_path=str(model_path),
        deterministic=True,
    )