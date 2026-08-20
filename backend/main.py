import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware

from backend.api.rate_limit import limiter
from backend.api.routes import router
from backend.services.betfair_auth import SessionExpiredError

load_dotenv()

FRONTEND = Path(__file__).resolve().parent.parent / "bet_automation.html"

# Placeholder shipped in .env.example. Usable for local development only — startup
# validation rejects it (and an unset key) when APP_ENV=production.
DEV_SESSION_SECRET = "dev-secret-change-in-production"

# Keys the app cannot function without: Betfair for market data and bet placement,
# OpenAI for parsing and ranking.
REQUIRED_ENV_VARS = ("BETFAIR_APP_KEY", "OPENAI_API_KEY")

APP_ENV = os.getenv("APP_ENV", "development").lower()
IS_PRODUCTION = APP_ENV == "production"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def validate_environment() -> None:
    """Fail at boot rather than at the first request that needs a missing key.

    Raises RuntimeError listing everything that's wrong, so a misconfigured
    deployment surfaces the whole problem at once instead of one key per restart.
    """
    problems = [f"{key} is not set" for key in REQUIRED_ENV_VARS if not os.getenv(key)]

    if IS_PRODUCTION and os.getenv("SESSION_SECRET_KEY", "") in ("", DEV_SESSION_SECRET):
        # Session cookies are signed with this key. Leaving it at the published
        # default lets anyone forge a session.
        problems.append(
            "SESSION_SECRET_KEY must be set to a private value when APP_ENV=production"
        )

    if problems:
        raise RuntimeError(
            "Invalid configuration — see .env.example:\n  - " + "\n  - ".join(problems)
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_environment()
    if not IS_PRODUCTION:
        logger.warning("Running with APP_ENV=%s — not hardened for production.", APP_ENV)
    yield


app = FastAPI(lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Signs each user's session cookie with this key — must be set in .env for production.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY") or DEV_SESSION_SECRET,
)

# The frontend is served same-origin by this app, so nothing needs cross-origin
# access in normal use. Kept configurable for deployments that split the two.
# Never widen to "*": combined with allow_credentials it makes Starlette reflect
# any requesting origin, handing every website a credentialed channel to the API.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def serve_frontend():
    return HTMLResponse(FRONTEND.read_text(encoding="utf-8"))
