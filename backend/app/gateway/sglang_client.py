"""Native SGLang requests for exact-prefix and latency measurements."""

import json
import os
from pathlib import Path
from time import perf_counter, sleep

import httpx
from dotenv import dotenv_values


def settings():
    from backend.app.gateway import registration
    if registration.enabled():
        return registration.connection()
    root = Path(__file__).resolve().parents[3]
    values = dotenv_values(root / ".env")
    url = (
        os.environ.get("SGLANG_BASE_URL")
        or values.get("SGLANG_BASE_URL")
        or "http://127.0.0.1:30000"
    )
    key = os.environ.get("SGLANG_API_KEY") or values.get("SGLANG_API_KEY")
    return url, key


def validate_counts(meta, token_count):
    for name in ("prompt_tokens", "cached_tokens", "completion_tokens"):
        if type(meta.get(name)) is not int or meta[name] < 0:
            raise ValueError("SGLang omitted a valid request-level token count.")
    if meta["prompt_tokens"] != token_count or meta["cached_tokens"] > meta["prompt_tokens"]:
        raise ValueError("SGLang cache count differs from the submitted token prefix.")


def sampling_params(max_new_tokens, *, stop_at_module_fence=False):
    params = {"temperature": 0, "max_new_tokens": max_new_tokens}
    if stop_at_module_fence:
        # Keep the model's closing delimiter for the downstream Python/HTTP checks.
        params.update(stop=["\n```\n", "\r\n```\r\n"], no_stop_trim=True)
    return params


class Engine(httpx.Client):
    def __init__(self, mode="live"):
        if mode == "hosted":
            from .hosted import endpoint
            url, key = endpoint()
        elif mode == "live":
            url, key = settings()
        else:
            raise ValueError("Select a live SGLang endpoint.")
        if not key:
            raise ValueError(
                "Configure SGLANG_API_KEY for the Cachet inference service."
            )
        from backend.app.gateway import registration
        transport = registration.TunnelTransport(url) if mode == "live" and registration.enabled() else None
        super().__init__(base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=180,
                         transport=transport, follow_redirects=False)

    def flush(self, *, timeout_s=10):
        self.post("/flush_cache", params={"timeout": timeout_s}).raise_for_status()

    def encode(self, text):
        body = {"prompt": text, "add_special_tokens": False}
        return self.post("/tokenize", json=body).raise_for_status().json()["tokens"]

    def decode(self, ids):
        body = {"tokens": ids, "skip_special_tokens": False}
        return self.post("/detokenize", json=body).raise_for_status().json()["text"]

    def sample(self, ids, max_new_tokens=8):
        body = {
            "input_ids": ids,
            "sampling_params": sampling_params(max_new_tokens),
        }
        result = self.post("/generate", json=body).raise_for_status().json()
        validate_counts(result["meta_info"], len(ids))
        return result

    def timed_sample(self, ids):
        result = self.generate(ids, max_new_tokens=8)
        return {
            key: result[key] for key in ("prompt_tokens", "cached_tokens", "ttft_ms")
        }

    def generate(self, ids, max_new_tokens, fault_ms=0, *, stop_at_module_fence=False):
        payload = {
            "input_ids": ids,
            "stream": True,
            "sampling_params": sampling_params(max_new_tokens, stop_at_module_fence=stop_at_module_fence),
        }
        # Serialize before starting the clock. Include transport and detokenization.
        content = json.dumps(payload).encode()
        first_text_ms = None
        engine_ttft_ms = None
        delay_ms = 0.0
        last_event = None
        done = False
        start = perf_counter()
        with self.stream(
            "POST",
            "/generate",
            content=content,
            headers={"Content-Type": "application/json"},
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                last_event = json.loads(data)
                if last_event.get("error"):
                    raise RuntimeError("SGLang rejected the streaming request")
                if first_text_ms is None and last_event.get("text"):
                    received = perf_counter()
                    engine_ttft_ms = (received - start) * 1000
                    if fault_ms:
                        sleep(fault_ms / 1000)
                        delay_ms = (perf_counter() - received) * 1000
                    first_text_ms = (perf_counter() - start) * 1000
        if first_text_ms is None or last_event is None:
            raise RuntimeError("SGLang returned no generated text")
        meta = last_event["meta_info"]
        validate_counts(meta, len(ids))
        reason = meta.get("finish_reason") or {}
        if not done or reason.get("type") not in {"stop", "length"}:
            raise RuntimeError("SGLang did not complete the stream successfully")
        return {
            "prompt_tokens": meta["prompt_tokens"],
            "cached_tokens": meta["cached_tokens"],
            "ttft_ms": round(first_text_ms, 3),
            "engine_ttft_ms": round(engine_ttft_ms, 3),
            "fault_injected_ms": round(delay_ms, 3),
            "total_ms": round((perf_counter() - start) * 1000, 3),
            "output_tokens": meta["completion_tokens"],
            "finish_reason": reason["type"],
            "text": last_event["text"],
        }
