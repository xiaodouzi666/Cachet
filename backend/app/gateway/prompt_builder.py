"""Construct and attest exact token spans used for inference and billing."""
import hashlib
import re
import struct
from dataclasses import dataclass

from backend.app.config import ROOT, sources

SYSTEM = "<|im_start|>system\nYou are a coding assistant. Repository context follows.\n"
END = "<|im_end|>\n"


def token_hash(ids):
    return hashlib.sha256(struct.pack(f"<{len(ids)}I", *ids)).hexdigest()


def dialogue(messages):
    parts = []
    for role, text in messages:
        if role not in {"assistant", "user"}:
            raise ValueError("Invalid dialogue role")
        # User and generated content are data, including any attempted role delimiters.
        parts.append(f"<|im_start|>{role}\n" + text.replace("<|", "< |") + END)
    return END + "".join(parts) + "<|im_start|>assistant\n"


@dataclass
class Prompt:
    ids: list[int]
    spans: list[dict]


class Prefix:
    def __init__(self, engine, slug):
        self.system = engine.encode(SYSTEM)
        entries = sources()
        chain = []
        while slug:
            row = entries[slug]
            chain.append(row)
            slug = row["parent"]
        self.layers = []
        for row in reversed(chain):
            text = (ROOT / row["text_path"]).read_bytes()
            if hashlib.sha256(text).hexdigest() != row["text_sha256"]:
                raise ValueError("Source text differs from licensed manifest")
            ids = engine.encode(text.decode())
            if token_hash(ids) != row["ids_sha256"]:
                raise ValueError("The running tokenizer differs from the source manifest")
            self.layers.append((row["slug"], ids))

    def build(self, engine, owners, tenant, messages, buy=True):
        if not re.fullmatch("[0-9a-f]{8}", tenant):
            raise ValueError("Invalid tenant salt")
        salt = engine.encode(f"\n<!-- tenant:{tenant} -->\n")
        parts = [("system","system",self.system)]
        parts += [("publisher",owners[slug],ids) for slug,ids in self.layers]
        if buy:
            parts.append(("private","salt",salt))
        else:
            parts = [("private","salt",salt)] + [("private","context",part[2]) for part in parts]
        parts.append(("private","turns",engine.encode(dialogue(messages))))
        ids, spans = [], []
        for kind, owner, section in parts:
            start = len(ids)
            ids.extend(section)
            spans.append(dict(kind=kind,owner=owner,start=start,end=len(ids)))
        return Prompt(ids,spans)

    def warm(self, engine):
        ids = self.system + [token for _,part in self.layers for token in part]
        engine.sample(ids,max_new_tokens=1)
        result = engine.sample(ids,max_new_tokens=1)["meta_info"]
        if result["prompt_tokens"] != len(ids):
            raise ValueError("SGLang probe length differs from prefix")
        return result["cached_tokens"] / len(ids)
