import os
import pytest
from unittest.mock import patch, DEFAULT

# Set before any test runs so the API secret dependency doesn't reject test requests.
os.environ.setdefault("API_SECRET_KEY", "test-secret")


@pytest.fixture(autouse=True)
def mock_logger(request):
    """Prevent any test from writing to the SQLite database.
    Skipped for test_logger.py which needs the real implementation."""
    if "test_logger" in request.node.fspath.basename:
        yield
    else:
        with patch.multiple(
            "backend.services.logger",
            log_slip_prepared=DEFAULT,
            log_bet_confirmed=DEFAULT,
            log_slip_expired=DEFAULT,
            log_failure=DEFAULT,
        ):
            yield
