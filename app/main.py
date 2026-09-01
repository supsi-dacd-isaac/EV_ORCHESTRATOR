from fastapi import FastAPI, Depends
from app.api.routes import auth, events, forecaster, sessions, database, admin_policies
from app.db.init_db import init_db
from app.services.common.auth import get_current_user

app = FastAPI(title="ORCHESTRATOR")


@app.on_event("startup")
def on_startup():
    init_db()

# Public routes (no authentication required)
app.include_router(auth.router)

# Protected routes (authentication required)
app.include_router(events.router, dependencies=[Depends(get_current_user)])
app.include_router(sessions.router, dependencies=[Depends(get_current_user)])
app.include_router(database.router, dependencies=[Depends(get_current_user)])
app.include_router(forecaster.router, dependencies=[Depends(get_current_user)])
app.include_router(admin_policies.admin_router, dependencies=[Depends(get_current_user)])
app.include_router(admin_policies.policies_router, dependencies=[Depends(get_current_user)])
app.include_router(admin_policies.charger_policy_router, dependencies=[Depends(get_current_user)])

@app.get("/health")
def health_check():
    return {"status": "OK"}