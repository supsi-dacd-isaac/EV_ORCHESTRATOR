# Configuration and Secret Conventions

## What is sensitive
- Secrets and credentials: `AUTH_SECRET_KEY`, DB username/password inside `DATABASE_URL`.
- Real user credentials for local debug scripts.


## What is configuration (not always secret)
- Runtime mode (`APP_ENV`), DB logging (`DB_ECHO`), token expiration, PBKDF2 iterations.
- File paths like `ACTOR_MODEL_PATH`.
- Host/port and deployment wiring in Docker Compose.

These should be environment-driven so each institute can set them without changing code.

## Current project convention
- Central app settings are in `app/config.py`.
- Runtime values are read from environment variables (and from `.env` locally).
- Local template is `.env.example` (safe defaults/placeholders only).
- Real `.env` is ignored by git via `.gitignore`.
- Docker Compose loads values from `.env` at the project root.
- `app/run_debug.py` is local-only and excluded from git/docker artifacts.

## Suggested deployment practice
- Build one image and deploy it to each environment with different runtime env vars.
- In production, inject secrets from a secret manager (Kubernetes Secret, Docker Swarm secret, Vault, etc.) instead of plain `.env` files.
- Rotate `AUTH_SECRET_KEY` if there is any chance it was ever exposed.
