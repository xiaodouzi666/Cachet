import os

import pytest

from backend.app.gateway.sglang_client import Engine, settings
from scripts.gpu_lease import gpu_lease


@pytest.fixture(autouse=True)
def serialize_physical_gpu_checks(request):
    if request.node.get_closest_marker("gpu"):
        with gpu_lease():
            yield
    else:
        yield


@pytest.fixture
def engine():
    if not settings()[1] and not os.getenv("CACHET_VERIFY_FULL"):
        pytest.skip("Set SGLANG_API_KEY to run dedicated GPU checks.")
    with Engine() as client:
        yield client
