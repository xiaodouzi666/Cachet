from ..money import allocate
from .shapley import shapley_values


def royalties(layers, pool_micros, method, *, alpha=None, beta=None, self_publishers=()):
    """Combine a publisher's layer shares, leaving self-attributed royalties with the platform."""
    if method not in {"linear", "shapley"}:
        raise ValueError("Unknown attribution method")
    lengths = [hits for _, hits in layers]
    if method == "shapley" and pool_micros:
        if alpha is None or beta is None:
            raise ValueError("Shapley pricing requires measured prefill coefficients")
        weights = shapley_values(lengths, alpha, beta)
    else:
        weights = lengths
    result = {}
    for (publisher, _), amount in zip(layers, allocate(pool_micros, weights)):
        result[publisher] = result.get(publisher, 0) + (0 if publisher in self_publishers else amount)
    return result
