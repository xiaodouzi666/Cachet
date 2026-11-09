"""Generate and verify the model's price explanation outside the paid coding dialogue."""

import json
import re
from datetime import UTC, datetime, timedelta
from math import isfinite

from backend.app.config import ROOT, manifest
from backend.app.gateway.prompt_builder import token_hash
from backend.app.replay.recordings import QUOTE_FIELDS, digest

ARCHIVE = ROOT / "backend/app/replay/quote_reasons.json"
OUTPUT_LIMIT = 96
STRATEGIES = {"buy": "shared prefix", "compute": "private computation"}


def canonical_quote(config):
    """Limit the model and public receipts to the non-private fields of the quote."""
    if not QUOTE_FIELDS <= config.keys() or config["decision"] not in STRATEGIES:
        raise ValueError("Missing fields or strategy in the pricing quote")
    quote = {key: config[key] for key in sorted(QUOTE_FIELDS)}
    for key in ("buy_micros", "compute_micros", "turns", "tokens"):
        if type(quote[key]) is not int or quote[key] < 0:
            raise ValueError("Invalid integral value in the pricing quote")
    for key in ("cold_ttft_ms", "warm_ttft_ms", "cold_fraction"):
        if type(quote[key]) not in (int, float) or not isfinite(quote[key]):
            raise ValueError("Non-finite value in the pricing quote")
    if (
        quote["turns"] < 1
        or quote["tokens"] < 1
        or not 0 <= quote["cold_fraction"] <= 1
        or min(quote["cold_ttft_ms"], quote["warm_ttft_ms"]) <= 0
    ):
        raise ValueError("Pricing quote has an invalid range")
    if quote[quote["decision"] + "_micros"] > min(
        quote["buy_micros"], quote["compute_micros"]
    ):
        raise ValueError("Selected strategy contradicts the quoted prices")
    return quote


def quote_key(quote):
    return digest(
        json.dumps(
            canonical_quote(quote), sort_keys=True, separators=(",", ":")
        ).encode()
    )


def options(quote):
    selected = quote["decision"]
    other = "compute" if selected == "buy" else "buy"
    return (
        STRATEGIES[selected],
        quote[selected + "_micros"],
        STRATEGIES[other],
        quote[other + "_micros"],
    )


def prompt_for(quote, retry):
    selected, price, alternative, other_price = options(quote)
    instruction = "Explain the price decision in the required two sentences."
    if retry:
        instruction = (
            "Write both sentences on one line, separated by a full stop and a space. "
            "Introduce each price with the word at. Make the second sentence start with "
            "The alternative is. End with a period."
        )
    return (
        # Start a distinct radix-cache branch, apart from both public and private coding prompts.
        "<|endoftext|><|im_start|>system\nYou turn structured prices into a brief English explanation. "
        "Write only two grammatical sentences. The first begins with 'I choose' and gives the selected option and its price. "
        "The second begins with 'The alternative' and gives the rejected option and its price. "
        "Use simple sentences with a verb before each price. End each sentence with one period. "
        "Use no semicolons or parentheses. Use 'microdollars' in each. Do not choose both. "
        "Use no commas within numbers. "
        "Do not mention speed, privacy or quality. If the two displayed prices are equal, report them without saying either is cheaper. "
        "<|im_end|>\n<|im_start|>user\n"
        f"Selected option: {selected}\nSelected price: {price} microdollars\n"
        f"Rejected option: {alternative}\nRejected price: {other_price} microdollars\n"
        + instruction
        + "<|im_end|>\n<|im_start|>assistant\n"
    )


def validate_reason(text, quote):
    selected, price, alternative, other_price = options(quote)
    # A full match rejects misplaced amounts, extra conclusions and a second 'I choose'.
    join = r"(?: at| for| because it costs|, which costs)"
    pattern = (
        rf"I choose (?:the )?{selected}(?: option)?{join} {price} microdollars\.\s+"
        rf"The alternative (?:is (?:the )?{alternative}(?: option)?(?: at| for)"
        rf"|(?:the )?{alternative}(?: option)? costs) {other_price} microdollars\."
    )
    if not isinstance(text, str) or not re.fullmatch(
        pattern, text.strip(), re.IGNORECASE
    ):
        raise ValueError(
            "Model explanation did not associate each strategy with its quoted price"
        )


def validate_evidence(receipt, quote):
    if (
        canonical_quote(receipt["quote"]) != quote
        or receipt["quote"].keys() != QUOTE_FIELDS
        or receipt["model"] != manifest()["tokenizer"]["model_id"]
    ):
        raise ValueError("Quote explanation belongs to another quote or model")
    if (
        type(receipt["retry"]) is not bool
        or not isinstance(receipt["generated_at"], str)
        or datetime.fromisoformat(receipt["generated_at"]).utcoffset() != timedelta(0)
    ):
        raise ValueError("Invalid quote explanation provenance")
    prompt = prompt_for(quote, receipt["retry"])
    ids = receipt["input_ids"]
    if (
        not isinstance(ids, list)
        or not ids
        or type(receipt["prompt_tokens"]) is not int
        or len(ids) != receipt["prompt_tokens"]
        or not all(type(i) is int and 0 <= i <= 0xFFFFFFFF for i in ids)
        or receipt["input_hash"] != token_hash(ids)
        or receipt["prompt_sha256"] != digest(prompt.encode())
        or receipt["answer_sha256"] != digest(receipt["reason"].encode())
    ):
        raise ValueError(
            "Quote explanation input or answer hash differs from its receipt"
        )
    for key in ("ttft_ms", "total_ms"):
        measurement = receipt[key]
        if (
            type(measurement) not in (int, float)
            or not isfinite(measurement)
            or measurement <= 0
        ):
            raise ValueError("Invalid quote explanation inference timing")
    if (
        receipt["finish_reason"] != "stop"
        or type(receipt["output_tokens"]) is not int
        or not 1 <= receipt["output_tokens"] <= OUTPUT_LIMIT
        or type(receipt["cached_tokens"]) is not int
        or not 0 <= receipt["cached_tokens"] <= len(ids)
        or receipt["ttft_ms"] > receipt["total_ms"]
    ):
        raise ValueError("Invalid quote explanation inference measurements")
    validate_reason(receipt["reason"], quote)


def generate_reason(engine, config):
    quote = canonical_quote(config)
    model = engine.get("/model_info").raise_for_status().json()["served_model_name"]
    if model != manifest()["tokenizer"]["model_id"]:
        raise ValueError("Quote explanation needs the pinned inference model")
    for retry in (False, True):
        prompt = prompt_for(quote, retry)
        ids = engine.encode(prompt)
        measured = engine.generate(ids, max_new_tokens=OUTPUT_LIMIT)
        reason = measured["text"]
        try:
            validate_reason(reason, quote)
        except ValueError:
            if retry:
                raise
            continue
        receipt = {
            key: measured[key]
            for key in (
                "prompt_tokens",
                "cached_tokens",
                "output_tokens",
                "finish_reason",
                "ttft_ms",
                "total_ms",
            )
        }
        receipt.update(
            reason=reason,
            source="live",
            quote=quote,
            model=model,
            retry=retry,
            input_ids=ids,
            input_hash=token_hash(ids),
            prompt_sha256=digest(prompt.encode()),
            answer_sha256=digest(reason.encode()),
            generated_at=datetime.now(UTC).isoformat(),
        )
        validate_evidence(receipt, quote)
        return receipt


def recorded_reason(config):
    quote = canonical_quote(config)
    try:
        receipt = json.loads(ARCHIVE.read_bytes())[quote_key(quote)]
        if receipt["source"] != "live":
            raise ValueError("Quote explanation was not recorded from live inference")
        validate_evidence(receipt, quote)
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        AttributeError,
    ) as exc:
        raise ValueError(
            "No verified model explanation for this exact recorded quote"
        ) from exc
    return {**receipt, "source": "recorded"}
