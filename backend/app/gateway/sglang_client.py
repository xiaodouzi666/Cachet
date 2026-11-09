"""Native SGLang requests for exact-prefix and latency measurements."""

import json
import os
from pathlib import Path
from time import perf_counter

import httpx
from dotenv import dotenv_values


def settings():
    root = Path(__file__).resolve().parents[3]
    values = dotenv_values(root / ".env")
    url = os.environ.get("SGLANG_BASE_URL") or values.get("SGLANG_BASE_URL") or "http://127.0.0.1:30000"
    key = os.environ.get("SGLANG_API_KEY") or values.get("SGLANG_API_KEY")
    return url, key


class Engine(httpx.Client):
    def __init__(self):
        url, key = settings()
        if not key:
            raise ValueError("Configure SGLANG_API_KEY for the Cachet inference service.")
        super().__init__(base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=180)

    def flush(self):
        self.post("/flush_cache", params={"timeout": 10}).raise_for_status()

    def encode(self, text):
        body = {"prompt": text, "add_special_tokens": False}
        return self.post("/tokenize", json=body).raise_for_status().json()["tokens"]

    def decode(self, ids):
        body = {"tokens": ids, "skip_special_tokens": False}
        return self.post("/detokenize", json=body).raise_for_status().json()["text"]

    def sample(self, ids, max_new_tokens=8):
        body = {"input_ids": ids, "sampling_params": {"temperature": 0, "max_new_tokens": max_new_tokens}}
        return self.post("/generate", json=body).raise_for_status().json()

    def timed_sample(self, ids):
        payload = {"input_ids": ids, "stream": True, "sampling_params": {"temperature": 0, "max_new_tokens": 8}}
        # Serialize before starting the clock. Include transport and detokenization.
        content = json.dumps(payload).encode()
        first_text_ms = None
        last_event = None
        done = False
        start = perf_counter()
        with self.stream("POST", "/generate", content=content, headers={"Content-Type": "application/json"}) as response:
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
                    first_text_ms = (perf_counter() - start) * 1000
        if first_text_ms is None or last_event is None:
            raise RuntimeError("SGLang returned no generated text")
        meta = last_event["meta_info"]
        reason = meta.get("finish_reason") or {}
        if not done or reason.get("type") not in {"stop", "length"}:
            raise RuntimeError("SGLang did not complete the stream successfully")
        return {
            "prompt_tokens": meta["prompt_tokens"],
            "cached_tokens": meta["cached_tokens"],
            "ttft_ms": round(first_text_ms, 3),
        }
