import os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from backend.api.routes import router
from backend.services.betfair_auth import SessionExpiredError

FRONTEND = Path(__file__).resolve().parent.parent / "bet_automation.html"

app = FastAPI()

# Signs each user's session cookie with this key — must be set in .env for production.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "dev-secret-change-in-production"),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Catches SessionExpiredError raised anywhere in the app and returns 401,
# which the frontend intercepts to re-show the login screen.
@app.exception_handler(SessionExpiredError)
async def session_expired_handler(_request: Request, exc: SessionExpiredError):
    return JSONResponse(status_code=401, content={"detail": str(exc)})

app.include_router(router)

@app.get("/")
def serve_frontend():
    return HTMLResponse(FRONTEND.read_text(encoding="utf-8"))
