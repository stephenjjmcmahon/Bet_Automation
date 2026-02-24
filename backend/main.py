
import os
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

load_dotenv()  # loads backend/.env if you run uvicorn from backend/

app = FastAPI()

BETFAIR_APP_KEY = os.getenv("BETFAIR_APP_KEY")
BETFAIR_SESSION_TOKEN = os.getenv("BETFAIR_SESSION_TOKEN")
BETFAIR_ENDPOINT = "https://api.betfair.com/exchange/betting/rest/v1.0/"

def betfair_post(path: str, payload: dict):
    if not BETFAIR_APP_KEY or not BETFAIR_SESSION_TOKEN:
        raise HTTPException(status_code=500, detail="Betfair credentials not configured")

    headers = {
        "X-Application": BETFAIR_APP_KEY,
        "X-Authentication": BETFAIR_SESSION_TOKEN,
        "Content-Type": "application/json",
    }


    url = BETFAIR_ENDPOINT + path
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Betfair request failed: {e}")
    r = requests.post(url, json={"filter": {}}, headers=headers, timeout=10)
    print("STATUS:", r.status_code)
    print("BODY:", r.text)
    r.raise_for_status()


@app.get("/api/event-types")
def event_types():
    # equivalent of your demo script: listEventTypes with empty filter
    return betfair_post("listEventTypes/", {"filter": {}})

print(repr(BETFAIR_SESSION_TOKEN))



