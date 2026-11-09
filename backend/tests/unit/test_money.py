from fractions import Fraction

import pytest

from backend.app.money import allocate, floor_cents, round_cents, usd_to_micros


def test_cent_boundary_and_exact_conversion():
    assert [round_cents(n) for n in (0, 4999, 5000, 5001, 14999, 15000, -5000)] == [0, 0, 1, 1, 1, 2, -1]
    assert floor_cents(19999) == 1
    assert usd_to_micros("0.004933") == 4933
    with pytest.raises(ValueError):
        usd_to_micros("0.0000001")


def test_largest_remainder_conserves_integer_pool():
    assert allocate(10, [Fraction(1, 3)] * 3) == [4, 3, 3]
    assert allocate(0, [0, 0]) == [0, 0]
    with pytest.raises(ValueError):
        allocate(1, [0, 0])
