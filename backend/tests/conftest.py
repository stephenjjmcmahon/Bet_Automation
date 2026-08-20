import os
from unittest.mock import DEFAULT, patch

import pytest

# Set before any test runs so the API secret dependency doesn't reject test requests.
os.environ.setdefault("API_SECRET_KEY", "test-secret")

# Tests hammer /api/prepare and /api/query from a single client address, which would
# otherwise trip the per-IP limits. The limits themselves are exercised separately
# in test_security.py, which re-enables the limiter for its own cases.
from backend.api.rate_limit import limiter  # noqa: E402

limiter.enabled = False


@pytest.fixture(autouse=True)
def mock_logger(request):
    """Prevent any test from writing to the SQLite database.
    Skipped for test_logger.py which needs the real implementation."""
    if "test_logger" in request.node.fspath.basename:
        yield None
    else:
        with patch.multiple(
            "backend.services.logger",
            log_slip_prepared=DEFAULT,
            log_bet_confirmed=DEFAULT,
            log_slip_expired=DEFAULT,
            log_failure=DEFAULT,
            log_search=DEFAULT,
            log_search_feedback=DEFAULT,
        ) as mocks:
            # Yielded so tests can assert on what was (or wasn't) logged.
            yield mocks
