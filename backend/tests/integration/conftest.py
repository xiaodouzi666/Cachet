import os

import pytest

from backend.app.gateway.sglang_client import Engine, settings


@pytest.fixture
def engine():
    if not settings()[1] and not os.getenv("CACHET_VERIFY_FULL"):
        pytest.skip("Set SGLANG_API_KEY to run dedicated GPU checks.")
    with Engine() as client:
        yield client
