import pytest

from backend.app.settlement.capture_policy import estimated_fee_cents, plan_capture


def test_normal_and_judge_capture():
    cap = 5_000_000
    assert plan_capture(1_999_999, cap, 2_000_000).amount_cents == 0
    assert plan_capture(2_009_999, cap, 2_000_000).amount_cents == 200
    assert plan_capture(59_999, cap, 2_000_000, force=True).amount_cents == 5
    assert plan_capture(9_999, cap, 2_000_000, force=True).amount_cents == 0
    assert estimated_fee_cents(5) == 5
    assert estimated_fee_cents(200) == 55
    assert estimated_fee_cents(0) == 0


@pytest.mark.parametrize(("remainder", "cents"), [(0, 0), (1, 0), (999_999, 0), (1_000_000, 100), (1_005_000, 101)])
def test_final_minimum_uses_unrounded_accrual(remainder, cents):
    assert plan_capture(remainder, 5_000_000, 2_000_000, final=True).amount_cents == cents


def test_single_capture_mode_and_authorization_guard():
    assert plan_capture(2_000_000, 5_000_000, 2_000_000, allow_partial=False).amount_cents == 0
    assert plan_capture(33_000, 40_000, 20_000, allow_partial=False, force=True).amount_cents == 3
    with pytest.raises(ValueError):
        plan_capture(40_001, 40_000, 20_000)
