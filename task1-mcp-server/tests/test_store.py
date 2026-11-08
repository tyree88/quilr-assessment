import pytest

from store import InsufficientBalance, get_customer, record_refund


def test_refund_reduces_balance_and_returns_receipt():
    refund = record_refund(get_customer("CUST-00001"), 100.0, "Damaged on arrival")
    assert refund.remaining_balance == 150.0
    assert refund.refund_id.startswith("RF-")
    assert get_customer("CUST-00001").balance == 150.0


def test_refund_of_full_balance_is_allowed():
    assert record_refund(get_customer("CUST-00001"), 250.0, "Order cancelled").remaining_balance == 0.0


def test_refund_over_balance_is_rejected_and_balance_unchanged():
    with pytest.raises(InsufficientBalance, match="exceeds refundable balance 250.00"):
        record_refund(get_customer("CUST-00001"), 400.0, "Customer complaint")
    assert get_customer("CUST-00001").balance == 250.0


def test_zero_balance_customer_cannot_be_refunded():
    with pytest.raises(InsufficientBalance):
        record_refund(get_customer("CUST-00003"), 10.0, "Goodwill gesture")


def test_balance_is_rounded_to_cents():
    assert record_refund(get_customer("CUST-00002"), 0.1, "Price adjustment").remaining_balance == 1200.4


def test_store_resets_between_tests():
    # Earlier tests refunded CUST-00001; the autouse fixture must have restored it.
    assert get_customer("CUST-00001").balance == 250.0
