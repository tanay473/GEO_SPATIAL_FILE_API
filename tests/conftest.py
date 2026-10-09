import os
import tempfile

# Point the app at a throwaway data dir before anything imports app.db.
os.environ["GEO_DATA_DIR"] = tempfile.mkdtemp(prefix="geo-api-test-")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def settings():
    return get_settings()
