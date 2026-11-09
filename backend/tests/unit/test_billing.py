from decimal import Decimal

import pytest

from backend.app.gateway.billing import Rates, Span, price_call, sla_breached


SPANS = [Span("system", "P0", 0, 300), Span("publisher", "L1", 300, 12300),
         Span("publisher", "L2", 12300, 26300), Span("private", "tenant", 26300, 27508)]


def bill(cached, **overrides):
    return price_call(spans=SPANS, cached_tokens=cached, output_tokens=250,
                      **({"method": "linear"} | overrides))


def test_specification_example_to_the_microdollar():
    result = bill(27100)
    assert result.components == dict(system=45, publisher=3900, private_hit=80, private_miss=408, output=500)
    assert (result.charge_micros, result.public_charge_micros, result.platform_micros) == (4933, 3945, 3893)
    assert result.royalty_micros == {"L1": 480, "L2": 560}
    assert bill(27100, method="shapley", alpha=1, beta=0).royalty_micros == {"L1": 760, "L2": 280}


def test_partial_full_and_cold_prefix():
    assert bill(0).charge_micros == 1208 + 500
    assert sum(bill(0).royalty_micros.values()) == 0
    assert bill(12300).royalty_micros == {"L1": 480, "L2": 0}
    assert bill(12300).public_charge_micros == 1845
    assert bill(27508).components["private_hit"] == 121
    assert bill(27508).components["private_miss"] == 0


def test_self_use_and_fractional_components_stay_balanced():
    res = bill(27100, method="shapley", alpha=1, beta=0, self_publishers={"L1"})
    assert res.royalty_micros == {"L1": 0, "L2": 280}
    assert res.platform_micros + sum(res.royalty_micros.values()) == res.charge_micros
    cfg = Rates.from_settings({"PRICE_FULL": "0.004", "PRICE_SHARED": "0.11"})
    fractional = bill(27100, rates=cfg)
    assert sum(fractional.components.values()) == fractional.charge_micros


def test_compute_only_and_sla_boundary():
    result = price_call(spans=[Span("private", "tenant", 0, 1000)], cached_tokens=500, output_tokens=0)
    assert result.charge_micros == 550
    assert result.public_charge_micros == 0
    assert not sla_breached(0, 201, 100)
    assert not sla_breached(1, 200, 100)
    assert sla_breached(1, 201, 100)


def test_invalid_meter_and_alignment():
    with pytest.raises(ValueError): bill(27509)
    with pytest.raises(ValueError): bill(-1)
    with pytest.raises(ValueError): bill(27508, method="shapley")
    with pytest.raises(ValueError): bill(27508, rates=Rates(shared="0.01"))
    with pytest.raises(ValueError):
        price_call(spans=[Span("private","x", 1, 3)], cached_tokens=0, output_tokens=0)
