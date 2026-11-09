import pytest

from bench.ttft_bench import benchmark


@pytest.mark.gpu
def test_warm_26k_prefix_has_lower_median_ttft(engine):
    result = benchmark(engine)
    assert result["warm_p50_ms"] < result["cold_p50_ms"], result
