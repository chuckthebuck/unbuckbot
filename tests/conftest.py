import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backend.app as backend
from backend.app import AppState


@pytest.fixture(autouse=True)
def reset_backend_state():
    backend.state = AppState()
    backend.REQUESTER_POLICIES = {}
    backend.WHITELIST_ONLY = True
    yield
