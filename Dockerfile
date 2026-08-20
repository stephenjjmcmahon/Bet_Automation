FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dependencies first so the layer caches across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Config comes from the environment (see .env.example) — no secrets are baked in.
#
# The app creates logs/ lazily on first write (backend/services/logger.py), so the
# runtime user has to own the working directory. That directory holds the SQLite bet
# database and the interpreter traces, and it is ephemeral unless mounted — see the
# volume in docker-compose.yml.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p logs \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
