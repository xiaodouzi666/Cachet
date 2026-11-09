"""USD amounts persisted by Cachet. PayPal operations operate on whole cents."""

from decimal import Decimal
from fractions import Fraction

MICROS_PER_USD = 1_000_000
MICROS_PER_CENT = 10_000


def as_fraction(value) -> Fraction:
    if isinstance(value, bool):
        raise ValueError("A monetary quantity must be numeric")
    if isinstance(value, Fraction):
        return value
    # Parse decimal notation, including JSON decimal rates passed as strings.
    return Fraction(Decimal(str(value)))


def half_up(value: Fraction) -> int:
    if value < 0:
        return -half_up(-value)
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def usd_to_micros(value) -> int:
    micros = as_fraction(value) * MICROS_PER_USD
    if micros.denominator != 1:
        raise ValueError("USD precision exceeds one microdollar")
    return int(micros)


def floor_cents(micros: int) -> int:
    return micros // MICROS_PER_CENT


def round_cents(micros: int) -> int:
    return half_up(Fraction(micros, MICROS_PER_CENT))


def allocate(total: int, weights) -> list[int]:
    """Divide integer money exactly. Tied remainders favor the earlier input."""
    if type(total) is not int or total < 0:
        raise ValueError("The pool must be nonnegative integer money")
    weights = [as_fraction(w) for w in weights]
    if any(w < 0 for w in weights):
        raise ValueError("Allocation weights must be nonnegative")
    weight_sum = sum(weights)
    if weight_sum == 0:
        if total:
            raise ValueError("Cannot divide a positive pool among zero weights")
        return [0] * len(weights)
    quotas = [total * w / weight_sum for w in weights]
    result = [int(q) for q in quotas]
    order = sorted(range(len(quotas)), key=lambda i: quotas[i] - result[i], reverse=True)
    for i in order[:total - sum(result)]:
        result[i] += 1
    return result
