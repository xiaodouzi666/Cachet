from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from backend.app.api.agent import slots
from backend.app.gateway.sglang_client import Engine, settings


@pytest.mark.gpu
def test_five_jobs_share_two_live_inference_slots():
    if not settings()[1]:
        import os

        if os.getenv("CACHET_VERIFY_FULL") == "1":
            pytest.fail("Full verification requires a configured live GPU")
        pytest.skip("Configure a GPU to verify simultaneous live requests")
    start = Barrier(6)
    two_running = Event()
    release = Event()
    guard = Lock()
    active = peak = 0

    def job(n):
        nonlocal active, peak
        start.wait(10)
        with slots:
            with guard:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    two_running.set()
            try:
                if not release.wait(15):
                    raise TimeoutError("Timed out waiting for the two available slots")
                with Engine() as engine:
                    prompt = engine.encode(
                        f"<|im_start|>user\nWhat is {n} plus one?<|im_end|>\n<|im_start|>assistant\n"
                    )
                    result = engine.generate(prompt, max_new_tokens=10)
                    return result["text"], result["prompt_tokens"]
            finally:
                with guard:
                    active -= 1

    with ThreadPoolExecutor(max_workers=5) as pool:
        work = [pool.submit(job, n) for n in range(5)]
        start.wait(10)
        try:
            assert two_running.wait(10)
            with guard:
                assert active == 2
        finally:
            release.set()
        results = [f.result(timeout=120) for f in work]
    assert peak == 2 and all(text.strip() and count > 0 for text, count in results)
