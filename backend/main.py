from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from backend.api.routes import router
from backend.services.betfair_auth import SessionExpiredError

app = FastAPI()


# Catches SessionExpiredError raised anywhere in the app and returns 401,
# which the frontend intercepts to re-show the login screen.
@app.exception_handler(SessionExpiredError)
async def session_expired_handler(_request: Request, exc: SessionExpiredError):
    return JSONResponse(status_code=401, content={"detail": str(exc)})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)