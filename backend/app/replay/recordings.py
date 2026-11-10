"""Validate checked-in GPU evidence before allowing it to drive a billed replay."""

import hashlib
import json
import math
import re
from datetime import datetime

from backend.app.agents.tasks import STEPS, TASKS, app_code, turn_question
from backend.app.agents.window import RECENT_TURNS, WINDOW_TOKENS, window_prompt
from backend.app.config import ROOT, manifest, sources
from backend.app.gateway.prompt_builder import END, SYSTEM, Prefix, dialogue, token_hash

DIRECTORY = ROOT / "backend/app/replay/traces"
SCHEMA = 8
SUPPORTED_SCHEMAS = (6, 7, SCHEMA)
SCHEMA_6_FIRST_STEP = (
    "In two sentences, explain the chosen strategy using the two quoted costs in microdollars."
)
HEX256 = re.compile("[0-9a-f]{64}")
QUOTE_FIELDS = {
    "decision",
    "buy_micros",
    "compute_micros",
    "cold_ttft_ms",
    "warm_ttft_ms",
    "turns",
    "tokens",
    "cold_fraction",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def path(task_id, buy):
    require(
        task_id in TASKS and type(buy) is bool,
        "Only built-in GPU sessions support replay",
    )
    return DIRECTORY / f"{task_id}-{'buy' if buy else 'compute'}.json"


def layers(task_id):
    slug = TASKS[task_id][0]
    by_slug = sources()
    chain = []
    while slug:
        row = by_slug[slug]
        source = manifest()["projects"][slug]
        raw = (ROOT / row["text_path"]).read_bytes()
        require(
            digest(raw) == row["text_sha256"],
            "Licensed source differs from the recording manifest",
        )
        chain.append(
            (
                slug,
                row["token_len"],
                row["ids_sha256"],
                source["revision"],
                manifest()["tokenizer"]["revision"],
            )
        )
        slug = row["parent"]
    return list(reversed(chain))


def recorded_question(schema, task, step, *, quote=None, previous="", http_feedback=None):
    require(
        type(schema) is int and schema in SUPPORTED_SCHEMAS,
        "Unsupported GPU recording format",
    )
    legacy_first_turn = schema == 6 and step == 0
    instruction = SCHEMA_6_FIRST_STEP if legacy_first_turn else STEPS[step]
    question = turn_question(
        task, step, instruction, previous=previous, http_feedback=http_feedback
    )
    if legacy_first_turn and quote is not None:
        return question + "\nQuote: " + json.dumps(quote, sort_keys=True)
    return question


def signature(task_id, buy, *, schema=SCHEMA):
    spec = [
        schema,
        TASKS[task_id],
        [recorded_question(schema, TASKS[task_id][1], i) for i in range(len(STEPS))],
        buy,
        SYSTEM,
        END,
        dialogue([("user", "<|"), ("assistant", "")]),
        layers(task_id),
        manifest()["tokenizer"],
    ]
    if schema >= 8:
        spec.append({"private_recent_turns": RECENT_TURNS, "max_window_tokens": WINDOW_TOKENS})
    return digest(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode())


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(record, task_id, buy, *, source_chain=None, request_quote=None):
    """All checks are offline. The digest of the complete raw trace is checked in load()."""
    require(
        type(record) is dict
        and type(record.get("schema")) is int
        and record["schema"] in SUPPORTED_SCHEMAS
        and integer(record.get("output_limit"), 1),
        "Unsupported GPU recording format or output limit",
    )
    chain = layers(task_id)
    require(
        record.get("task_id") == task_id
        and record.get("buy") is buy
        and record.get("source") == TASKS[task_id][0]
        and record.get("signature") == signature(task_id, buy, schema=record["schema"])
        and record.get("model") == manifest()["tokenizer"]["model_id"]
        and type(record.get("tenant")) is str
        and re.fullmatch("[0-9a-f]{8}", record["tenant"]),
        "Recorded session differs from its model, task or source specification",
    )
    stamp = datetime.fromisoformat(record["recorded_at"])
    require(stamp.utcoffset() is not None, "Recording timestamp needs a timezone")
    if source_chain is not None:
        require(
            list(map(tuple, source_chain)) == chain,
            "Published context chain differs from this recording",
        )
    q = record["quote"]
    require(
        type(q) is dict
        and set(q) == QUOTE_FIELDS
        and q["turns"] == len(STEPS)
        and q["tokens"] == sum(row[1] for row in chain)
        and integer(q["buy_micros"])
        and integer(q["compute_micros"])
        and finite(q["cold_fraction"])
        and 0 <= q["cold_fraction"] <= 1
        and all(finite(q[k]) and q[k] > 0 for k in ("cold_ttft_ms", "warm_ttft_ms"))
        and q["decision"] == ("buy" if buy else "compute")
        and buy == (q["buy_micros"] <= q["compute_micros"]),
        "Invalid quote in GPU recording",
    )
    if request_quote is not None:
        require(
            all(request_quote.get(k) == q[k] for k in QUOTE_FIELDS),
            f"The selected quote differs from this GPU recording (recorded cold fraction {q['cold_fraction']}).",
        )
    turns = record["turns"]
    require(type(turns) is list and len(turns) == len(STEPS), "Incomplete GPU session")
    expected = (
        [
            ("system", "system", None),
            *[("publisher", r[0], r[1]) for r in chain],
            ("private", "salt", None),
        ]
        if buy
        else [
            ("private", "salt", None),
            ("private", "context", None),
            *[("private", "context", r[1]) for r in chain],
        ]
    )
    expected.append(("private", "turns", None))
    prior_spans = None
    seen_inputs = set()
    previous_prompt = 0
    for i, turn in enumerate(turns, 1):
        require(
            type(turn) is dict
            and turn.get("kind") == "result"
            and turn.get("mode") == "live"
            and integer(turn.get("turn"), 1)
            and turn["turn"] == i
            and isinstance(turn.get("text"), str)
            and turn["text"].strip()
            and integer(turn.get("prompt_tokens"), 1 if record["schema"] >= 8 else previous_prompt + 1)
            and integer(turn.get("cached_tokens"))
            and turn["cached_tokens"] <= turn["prompt_tokens"]
            and turn.get("finish_reason") == "stop"
            and integer(turn.get("output_tokens"), 1)
            and turn["output_tokens"] <= record["output_limit"]
            and turn["prompt_tokens"] + record["output_limit"]
            <= (min(WINDOW_TOKENS, manifest()["context_window"]) if record["schema"] >= 8 else manifest()["context_window"])
            and all(
                finite(turn.get(k)) and turn[k] > 0
                for k in ("engine_ttft_ms", "ttft_ms", "total_ms")
            )
            and turn["engine_ttft_ms"] <= turn["ttft_ms"] <= turn["total_ms"]
            and turn.get("fault_injected_ms") == 0
            and type(turn.get("input_hash")) is str
            and HEX256.fullmatch(turn["input_hash"])
            and turn["input_hash"] not in seen_inputs,
            "Invalid native GPU metering or turn order",
        )
        seen_inputs.add(turn["input_hash"])
        previous_prompt = turn["prompt_tokens"]
        spans = turn["spans"]
        require(
            type(spans) is list and len(spans) == len(expected),
            "Recorded prompt has unexpected spans",
        )
        pos = 0
        for span, (kind, owner, size) in zip(spans, expected):
            require(
                type(span) is dict
                and set(span) == {"kind", "owner", "start", "end"}
                and span["kind"] == kind
                and span["owner"] == owner
                and integer(span["start"])
                and span["start"] == pos
                and integer(span["end"], pos + 1)
                and (size is None or span["end"] - pos == size),
                "Invalid source attribution in recording",
            )
            pos = span["end"]
        require(
            pos == turn["prompt_tokens"]
            and (prior_spans is None or spans[:-1] == prior_spans),
            "Recorded source spans drift between turns",
        )
        prior_spans = spans[:-1]
    final = record.get("application_check")
    validate_application(task_id, final, turns[-1]["text"])
    selected = record.get("verified_application_turn")
    if final["status"] == "passed":
        require(selected == len(turns), "Missing final application selection")
    elif selected is not None:
        require(
            type(selected) is int and 1 <= selected < len(turns),
            "Invalid selected application turn",
        )
        selected_turn = turns[selected - 1]
        prior = record.get("earlier_application_check")
        validate_application(task_id, prior, selected_turn["text"], draft=True)
        require(
            prior["status"] == "passed",
            "Earlier selected application failed its HTTP contract",
        )
    return record


def validate_application(task_id, check, answer, *, draft=False):
    require(
        isinstance(check, dict)
        and check.get("answer_sha256") == digest(answer.encode()),
        "Application check does not match the generated final answer",
    )
    try:
        app_code(answer)
        conforms = True
    except ValueError:
        conforms = False
    require(
        check.get("requested_format_met") is conforms,
        "Incorrect application message-format evidence",
    )
    if check.get("status") == "passed":
        expected = (
            {
                "status_codes": [200, 200, 429, 200, 200],
                "success_body": {"pong": True},
                "distinct_clients": 2,
                "window_seconds": 60,
            }
            if task_id == "rate-limit"
            else {
                "status_codes": [200, 404, 200],
                "success_body": {"pong": True},
                "x_route_class": "custom",
                "subclass_probe": True,
            }
        )
        require(
            check.get("source_sha256")
            == digest(app_code(answer, allow_prose=True).encode())
            and check.get("http") == expected,
            "Incomplete HTTP evidence for final application",
        )
    else:
        require(
            check.get("status") == "failed"
            and isinstance(check.get("reason"), str)
            and check["reason"].strip(),
            "Missing result from generated application check",
        )


def verify_inputs(engine, record):
    """Rebuild the original input IDs from the licensed sources and previous GPU completions."""
    task_id, buy = record["task_id"], record["buy"]
    prefix = Prefix(engine, TASKS[task_id][0])
    owners = {slug: slug for slug, _ in prefix.layers}
    messages = []
    for i, turn in enumerate(record["turns"]):
        question = recorded_question(
            record["schema"],
            TASKS[task_id][1],
            i,
            quote=record["quote"],
            previous=messages[-1][1] if messages else "",
            http_feedback=record["turns"][i - 1].get("http_feedback") if i else None,
        )
        messages.append(("user", question))
        if record["schema"] >= 8:
            prompt = window_prompt(engine, prefix, owners, record["tenant"], messages, buy, record["output_limit"])
        else:
            prompt = prefix.build(engine, owners, record["tenant"], messages, buy)
        require(
            turn["spans"] == prompt.spans
            and turn["prompt_tokens"] == len(prompt.ids)
            and turn["input_hash"] == token_hash(prompt.ids),
            "Reconstructed GPU input IDs do not match",
        )
        messages.append(("assistant", turn["text"]))


def checked_json(raw, expected_sha256):
    require(digest(raw) == expected_sha256, "GPU recording checksum mismatch")
    return json.loads(raw)


def load(task_id, buy, *, source_chain=None, request_quote=None):
    location = path(task_id, buy)
    try:
        hashes = json.loads((DIRECTORY / "sha256.json").read_bytes())
        raw = location.read_bytes()
        record = checked_json(raw, hashes[location.name])
        return validate(
            record, task_id, buy, source_chain=source_chain, request_quote=request_quote
        )
    except (KeyError, TypeError, OverflowError, OSError) as exc:
        raise ValueError(
            "GPU recording or checksum manifest is missing or malformed"
        ) from exc


def application_evidence(track=None):
    if track is None:
        return {
            "application_check": {
                "status": "unverified",
                "reason": "This live run was not executed in the Docker HTTP checker.",
            },
            "verified_application_turn": None,
        }
    return {
        k: track[k]
        for k in (
            "application_check",
            "verified_application_turn",
            "earlier_application_check",
        )
        if k in track
    }


def available():
    from backend.app.agents.quote_reason import recorded_reason

    try:
        return all(
            recorded_reason(load(task_id, buy)["quote"])
            for task_id in TASKS
            for buy in (True, False)
        )
    except ValueError:
        return False
