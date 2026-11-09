"""Check the licensed source bytes behind the pinned layer token counts, offline."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "contexts/manifest.json").read_text(encoding="utf-8"))


def test_original_license_files():
    for project in MANIFEST["projects"].values():
        license_bytes = (ROOT / project["license_path"]).read_bytes()
        assert hashlib.sha256(license_bytes).hexdigest() == project["license_sha256"]


def test_published_contexts_fit_with_conversation_reserve():
    base, extension = MANIFEST["contexts"]
    assert (base["slug"], base["parent"]) == ("starlette", None)
    assert (extension["slug"], extension["parent"]) == ("fastapi", base["slug"])
    assert 0 < base["token_len"] <= 12_000
    assert 0 < extension["token_len"] <= 14_000
    assert MANIFEST["reserved_tokens"] >= 4_096
    assert MANIFEST["context_window"] == 32_768
    assert base["token_len"] + extension["token_len"] + MANIFEST["reserved_tokens"] <= MANIFEST["context_window"]


def test_source_headers_excerpts_and_published_text_match_manifest():
    for layer in MANIFEST["contexts"]:
        text = (ROOT / layer["text_path"]).read_bytes()
        assert text.endswith(b"\n")
        assert hashlib.sha256(text).hexdigest() == layer["text_sha256"]
        lines = iter(text.splitlines(keepends=True))
        for index, source in enumerate(layer["files"]):
            if index:
                assert next(lines) == b"\n"
            start, end = source["start_line"], source["end_line"]
            assert next(lines) == f"# Source: {source['path']} (lines {start}-{end})\n".encode()
            excerpt = b"".join(next(lines) for _ in range(end - start + 1))
            assert hashlib.sha256(excerpt).hexdigest() == source["excerpt_sha256"]
        assert next(lines, None) is None
