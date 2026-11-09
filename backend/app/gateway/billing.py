"""Intersect engine-reported prefix hits with exact token boundaries and price the call."""

from dataclasses import dataclass
from fractions import Fraction
from math import isfinite

from ..money import allocate, as_fraction, half_up
from .attribution import royalties


@dataclass(frozen=True)
class Span:
    kind: str
    owner: str
    start: int
    end: int


@dataclass(frozen=True)
class Rates:
    full: str = "1.00"
    private: str = "0.10"
    shared: str = "0.15"
    royalty: str = "0.04"
    output: str = "2.00"

    @classmethod
    def from_settings(cls, values):
        return cls(*(values[k] if values.get(k) not in (None, "") else default for k, default in zip(
            ("PRICE_FULL", "PRICE_SELF", "PRICE_SHARED", "ROYALTY_RATE", "PRICE_OUT"),
            ("1.00", "0.10", "0.15", "0.04", "2.00")
        )))


@dataclass(frozen=True)
class BillingResult:
    charge_micros: int
    public_charge_micros: int
    royalty_micros: dict[str, int]
    platform_micros: int
    hits: dict[str, int]
    components: dict[str, int]


def price_call(*, spans, cached_tokens: int, output_tokens: int, rates=Rates(),
               method="shapley", alpha=None, beta=None, self_publishers=()) -> BillingResult:
    full, private, shared, royalty, output = [as_fraction(v) for v in
                                             (rates.full, rates.private, rates.shared, rates.royalty, rates.output)]
    if min(full, private, shared, royalty, output) < 0 or royalty > shared:
        raise ValueError("Invalid token pricing")
    if type(cached_tokens) is not int or type(output_tokens) is not int or min(cached_tokens, output_tokens) < 0:
        raise ValueError("Invalid engine token counts")
    components = dict(system=Fraction(), publisher=Fraction(), private_hit=Fraction(),
                      private_miss=Fraction(), output=output_tokens * output)
    offset = 0
    hits = {}
    layers = []
    seen_private = False
    for span in spans:
        if (type(span.start) is not int or type(span.end) is not int or span.start != offset
                or span.end < span.start or not span.owner):
            raise ValueError("Spans must form a contiguous partition from zero")
        offset = span.end
        hit = max(0, min(cached_tokens, span.end) - span.start)
        hits[span.owner] = hits.get(span.owner, 0) + hit
        if span.kind == "private":
            seen_private = True
            components["private_hit"] += hit * private
            components["private_miss"] += (span.end - span.start - hit) * full
        elif span.kind in {"system", "publisher"}:
            if seen_private:
                raise ValueError("Public spans must precede private content")
            components[span.kind] += hit * shared
            if span.kind == "publisher":
                layers.append((span.owner, hit))
        else:
            raise ValueError("Unknown span kind")
    if cached_tokens > offset:
        raise ValueError("Engine cache count exceeds the submitted prompt")
    charge = half_up(sum(components.values()))
    components = dict(zip(components, allocate(charge, components.values())))
    pool = min(components["publisher"], half_up(sum(h for _, h in layers) * royalty))
    shares = royalties(layers, pool, method, alpha=alpha, beta=beta, self_publishers=self_publishers)
    return BillingResult(charge, components["system"] + components["publisher"], shares,
                         charge - sum(shares.values()), hits, components)


def sla_breached(public_hits: int, ttft_ms: float, warm_p50_ms: float, factor=2) -> bool:
    if not all(isfinite(v) for v in (ttft_ms, warm_p50_ms, factor)) or min(warm_p50_ms, factor) <= 0 or ttft_ms < 0:
        raise ValueError("Expected measured latency and a positive SLA factor")
    return public_hits > 0 and ttft_ms > factor * warm_p50_ms
