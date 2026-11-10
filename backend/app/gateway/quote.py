from decimal import Decimal, ROUND_HALF_UP
from backend.app.config import band_for, value


def quote(tokens, turns, cold_fraction, profile=None):
    cold = Decimal(str(cold_fraction))
    if not 0 <= cold <= 1 or not 1 <= turns <= 30:
        raise ValueError("Cold fraction must be 0–1 and turns must be 1–30")
    amount = Decimal(tokens * turns)
    baseline = amount * (
        cold * Decimal(value("PRICE_FULL", "1"))
        + (1 - cold) * Decimal(value("PRICE_SELF", "0.10"))
    )
    shared = amount * Decimal(value("PRICE_SHARED", "0.15"))
    band = band_for(tokens, profile)
    warm_ms = Decimal(str(band["warm_p50_ms"]))
    cold_ms = Decimal(str(band["cold_p50_ms"]))
    return {
        "decision": "buy" if shared <= baseline else "compute",
        "buy_micros": int(shared.to_integral_value(rounding=ROUND_HALF_UP)),
        "compute_micros": int(baseline.to_integral_value(rounding=ROUND_HALF_UP)),
        "cold_ttft_ms": band["cold_p50_ms"],
        "warm_ttft_ms": band["warm_p50_ms"],
        "buy_ttft_ms": float(warm_ms),
        "compute_ttft_ms": float(cold * cold_ms + (1 - cold) * warm_ms),
        "tokens": tokens,
        "turns": turns,
        "cold_fraction": float(cold),
    }
