from fastapi import FastAPI
from app.api.routes import events, sessions

app = FastAPI(title="ORCHESTRATOR")

app.include_router(events.router)
app.include_router(sessions.router)

@app.get("/health")
def health_check():
    return {"status": "OK"}