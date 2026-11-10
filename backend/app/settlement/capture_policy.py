from dataclasses import dataclass
from fractions import Fraction

from ..money import MICROS_PER_CENT, floor_cents, half_up, round_cents


@dataclass(frozen=True)
class CapturePlan:
    amount_cents: int
    final: bool


def plan_capture(pending_micros: int, remaining_micros: int, threshold_micros: int, *,
                 final=False, force=False, allow_partial=True, final_min_micros=1_000_000) -> CapturePlan:
    if min(pending_micros, remaining_micros, threshold_micros, final_min_micros) < 0 or remaining_micros % MICROS_PER_CENT:
        raise ValueError("Invalid accrual, authorization or threshold")
    if pending_micros > remaining_micros:
        raise ValueError("The accrued balance exceeds the authorization")
    if final:
        return CapturePlan(round_cents(pending_micros) if pending_micros >= final_min_micros else 0, True)
    if not force and (not allow_partial or pending_micros < threshold_micros):
        return CapturePlan(0, False)
    return CapturePlan(floor_cents(pending_micros), False)


def estimated_fee_cents(amount_cents):
    """US sandbox estimate; settlement always uses PayPal's measured fee."""
    if amount_cents < 0:
        raise ValueError("Capture amount cannot be negative")
    return min(amount_cents, 49 + half_up(Fraction(amount_cents * 299, 10_000)))
