from decimal import Decimal

import pytest

from backend.app.config import band_for
from backend.app.gateway.quote import quote


@pytest.mark.parametrize(
    "fraction, expected_micros, decision",
    [(Decimal("0"), 25_900, "compute"), (Decimal(".25"), 84_175, "buy"), (Decimal("1"), 259_000, "buy")],
)
def test_two_layer_quote_prices_and_per_turn_ttft(fraction, expected_micros, decision, monkeypatch):
    # The pricing example uses the published default rates regardless of local deployment overrides.
    for key, amount in (("PRICE_FULL", "1"), ("PRICE_SELF", ".10"), ("PRICE_SHARED", ".15")):
        monkeypatch.setenv(key, amount)
    result = quote(25_900, 10, fraction)
    assert (result["buy_micros"], result["compute_micros"], result["decision"]) == (38_850, expected_micros, decision)
    band = band_for(25_900)
    assert result["buy_ttft_ms"] == band["warm_p50_ms"]
    warm = Decimal(str(band["warm_p50_ms"]))
    cold = Decimal(str(band["cold_p50_ms"]))
    assert result["compute_ttft_ms"] == float(warm * (1 - fraction) + cold * fraction)
