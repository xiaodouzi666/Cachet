"""Native SGLang requests for exact-prefix and latency measurements."""

import os
from pathlib import Path

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

    def sample(self, ids, max_new_tokens=8):
        body = {"input_ids": ids, "sampling_params": {"temperature": 0, "max_new_tokens": max_new_tokens}}
        return self.post("/generate", json=body).raise_for_status().json()
