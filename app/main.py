from fastapi import FastAPI
from app.api.routes import events, sessions, database

app = FastAPI(title="ORCHESTRATOR")

app.include_router(events.router)
app.include_router(sessions.router)
app.include_router(database.router)

@app.get("/health")
def health_check():
    return {"status": "OK"}