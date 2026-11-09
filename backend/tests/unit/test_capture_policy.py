import pytest

from backend.app.settlement.capture_policy import plan_capture


def test_partial_threshold_and_close_rounding():
    assert plan_capture(19999, 5000000, 20000).amount_cents == 0
    assert plan_capture(20001, 5000000, 20000).amount_cents == 2
    assert plan_capture(19999, 5000000, 20000, force=True).amount_cents == 1
    assert plan_capture(5000, 5000000, 20000, final=True).amount_cents == 1
    assert plan_capture(4999, 5000000, 20000, final=True).amount_cents == 0


def test_single_capture_mode_and_authorization_guard():
    assert plan_capture(33000, 40000, 20000, allow_partial=False, force=True).amount_cents == 0
    assert plan_capture(33000, 40000, 20000, allow_partial=False, final=True).amount_cents == 3
    with pytest.raises(ValueError): plan_capture(40001, 40000, 20000)
