import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # repo root, so `import app.main` works

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as m


@pytest.fixture()
def client():
    """App wired to an in-memory fake MongoDB (fast unit tests for rules + CRUD; analytics need a real MongoDB)."""
    m.db = mongomock.MongoClient(tz_aware=True)["t"]
    m._indexes_ready = False
    m.ensure_indexes()
    return TestClient(m.app)
