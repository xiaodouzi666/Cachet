from dataclasses import dataclass

from ..money import MICROS_PER_CENT, floor_cents, round_cents


@dataclass(frozen=True)
class CapturePlan:
    amount_cents: int
    final: bool


def plan_capture(pending_micros: int, remaining_micros: int, threshold_micros: int, *,
                 final=False, force=False, allow_partial=True) -> CapturePlan:
    if min(pending_micros, remaining_micros, threshold_micros) < 0 or remaining_micros % MICROS_PER_CENT:
        raise ValueError("Invalid accrual, authorization or threshold")
    if pending_micros > remaining_micros:
        raise ValueError("The accrued balance exceeds the authorization")
    if final:
        return CapturePlan(round_cents(pending_micros), True)
    if not allow_partial or (not force and pending_micros < threshold_micros):
        return CapturePlan(0, False)
    return CapturePlan(floor_cents(pending_micros), False)
