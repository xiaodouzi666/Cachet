import httpx
import pytest


@pytest.mark.gpu
def test_generate_requires_key_and_answers_three_requests(engine):
    prompt = "Continue counting: one, two,"
    body = {"text": prompt, "sampling_params": {"temperature": 0, "max_new_tokens": 8}}
    # No client Authorization header for this call.
    assert httpx.post(engine.base_url.join("/generate"), json=body, timeout=30).status_code in {401, 403}
    for _ in range(3):
        result = engine.post("/generate", json=body).raise_for_status().json()
        assert result["text"].strip()
        assert result["meta_info"]["completion_tokens"] > 0
