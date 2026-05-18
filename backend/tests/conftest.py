import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def mock_logger(request):
    """Prevent any test from writing to the SQLite database.
    Skipped for test_logger.py which needs the real implementation."""
    if "test_logger" in request.node.fspath.basename:
        yield
    else:
        with patch("backend.services.logger.log_event"):
            yield
