"""Reproduce the exact token count and hash of each published layer."""

import argparse
import hashlib
import json
import struct
from pathlib import Path

from tokenizers import Tokenizer

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tokenizer_json", type=Path, help="Pinned Qwen tokenizer.json")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "contexts/manifest.json").read_text(encoding="utf-8"))
    expected = manifest["tokenizer"]["vocabulary_sha256"]
    if hashlib.sha256(args.tokenizer_json.read_bytes()).hexdigest() != expected:
        raise SystemExit("Tokenizer digest differs from the pinned Qwen release.")
    tokenizer = Tokenizer.from_file(str(args.tokenizer_json))
    for layer in manifest["contexts"]:
        raw = (ROOT / layer["text_path"]).read_bytes()
        ids = tokenizer.encode(raw.decode("utf-8"), add_special_tokens=False).ids
        token_hash = hashlib.sha256(struct.pack(f"<{len(ids)}I", *ids)).hexdigest()
        if (
            len(ids) != layer["token_len"]
            or token_hash != layer["ids_sha256"]
            or hashlib.sha256(raw).hexdigest() != layer["text_sha256"]
            or tokenizer.decode(ids, skip_special_tokens=False).encode("utf-8") != raw
        ):
            raise SystemExit(f"{layer['slug']}: text, token count, token hash or decode mismatch")
        print(f"{layer['slug']}: {len(ids):,} tokens, SHA-256 {token_hash}")


if __name__ == "__main__":
    main()
