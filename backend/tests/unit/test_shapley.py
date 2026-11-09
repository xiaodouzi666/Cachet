from fractions import Fraction

import pytest

from backend.app.gateway.shapley import prefix_value, shapley_values


def test_linear_cost_for_ordered_layers():
    assert shapley_values([12000, 14000], 1, 0) == (19000, 7000)
    assert prefix_value([12000, 14000], 2, 1, 0) == 0


def test_symmetry_of_equal_marginal_contributions():
    # Both players are necessary to realize any work when the root is a zero-length gate.
    assert shapley_values([0, 8], 1, 0) == (4, 4)
    # Equal lengths in an ordered chain have different marginal contributions.
    assert shapley_values([8, 8], 1, 0) == (12, 4)


def test_efficiency_and_limit():
    layers = [18, 22, 105, 0, 0, 0]
    values = shapley_values(layers, Fraction(2, 3), Fraction(1, 100))
    assert sum(values) == prefix_value(layers, 63, Fraction(2, 3), Fraction(1, 100))
    assert values[3:] == (0, 0, 0)
    with pytest.raises(ValueError): shapley_values([1] * 7, 1, 0)
