"""Exact Shapley allocation of the prefill work avoided by a prefix chain."""

from fractions import Fraction
from math import comb

from ..money import as_fraction


def prefix_value(lengths, mask: int, alpha, beta) -> Fraction:
    n = 0
    for i, tokens in enumerate(lengths):
        if not mask & (1 << i):
            break
        n += tokens
    return alpha * n + beta * n * n


def shapley_values(lengths, alpha, beta) -> tuple[Fraction, ...]:
    lengths = tuple(lengths)
    count = len(lengths)
    if count > 6 or any(type(n) is not int or n < 0 for n in lengths):
        raise ValueError("Expected up to six nonnegative layer lengths")
    alpha, beta = as_fraction(alpha), as_fraction(beta)
    if min(alpha, beta) < 0 or alpha + beta <= 0:
        raise ValueError("Expected a positive, non-decreasing prefill cost curve")
    values = [prefix_value(lengths, mask, alpha, beta) for mask in range(1 << count)]
    shares = []
    for i in range(count):
        bit = 1 << i
        phi = Fraction(0)
        for mask in range(1 << count):
            if not mask & bit:
                weight = Fraction(1, count * comb(count - 1, mask.bit_count()))
                phi += weight * (values[mask | bit] - values[mask])
        shares.append(phi)
    return tuple(shares)
