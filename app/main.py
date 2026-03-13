from fastapi import FastAPI, Depends
from app.api.routes import auth, events, forecaster, sessions, database
from app.services.common.auth import get_current_user

app = FastAPI(title="ORCHESTRATOR")

# Public routes (no authentication required)
app.include_router(auth.router)

# Protected routes (authentication required)
app.include_router(events.router, dependencies=[Depends(get_current_user)])
app.include_router(sessions.router, dependencies=[Depends(get_current_user)])
app.include_router(database.router, dependencies=[Depends(get_current_user)])
app.include_router(forecaster.router, dependencies=[Depends(get_current_user)])

@app.get("/health")
def health_check():
    return {"status": "OK"}