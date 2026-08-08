"""Per-IP rate limiting for the endpoints that cost money or guard credentials.

Lives in its own module so `routes.py` can decorate handlers with the same limiter
instance that `main.py` registers on the app, without importing `main` (which
imports `routes`).

Limits are in-memory, so they reset on restart and are per-process — fine for the
single-instance local deployment this app targets. A multi-worker deployment would
need a shared backend (`storage_uri=redis://...`).
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# /api/query and /api/prepare both call OpenAI on every request, so an exposed
# instance without a cap is a direct route to burning the API key.
AI_RATE_LIMIT = os.getenv("AI_RATE_LIMIT", "30/minute")

# Slows credential stuffing against the Betfair login proxy.
LOGIN_RATE_LIMIT = os.getenv("LOGIN_RATE_LIMIT", "10/minute")

limiter = Limiter(key_func=get_remote_address)
