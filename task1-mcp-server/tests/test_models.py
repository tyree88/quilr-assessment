import pytest
from pydantic import ValidationError

from models import GetCustomerRequest, RefundRequest

VALID = {"customer_id": "CUST-00001", "amount": 40.0, "reason": "Damaged on arrival"}


@pytest.mark.parametrize(
    "customer_id",
    [
        "cust-00001",  # lowercase prefix
        "CUST-0001",  # 4 digits
        "CUST-000001",  # 6 digits
        " CUST-00001",  # leading space
        "CUST-00001\n",  # trailing newline: Python re's "$" would accept it
        "CUST-٠٠٠٠١",  # Arabic-Indic digits: "\d" would accept them
        "CUST-ABCDE",  # letters
        "XCUST-00001",  # prefix not anchored
        "",
        1,  # not a string
        None,
    ],
)
def test_customer_id_rejects_malformed(customer_id):
    with pytest.raises(ValidationError):
        GetCustomerRequest.model_validate({"customer_id": customer_id})


def test_customer_id_accepts_valid():
    assert GetCustomerRequest.model_validate({"customer_id": "CUST-00001"}).customer_id == "CUST-00001"


@pytest.mark.parametrize(
    "amount",
    [0, -5, float("nan"), float("inf"), 1_000_000.01, "10", True, None],
)
def test_amount_rejects_malformed(amount):
    with pytest.raises(ValidationError):
        RefundRequest.model_validate({**VALID, "amount": amount})


@pytest.mark.parametrize("amount", [0.01, 10, 1_000_000])
def test_amount_accepts_valid(amount):
    assert RefundRequest.model_validate({**VALID, "amount": amount}).amount == amount


@pytest.mark.parametrize("reason", ["too short", "   short    ", "", 12345678901, None])
def test_reason_rejects_malformed(reason):
    with pytest.raises(ValidationError):
        RefundRequest.model_validate({**VALID, "reason": reason})


def test_reason_is_stripped_before_length_check():
    assert RefundRequest.model_validate({**VALID, "reason": "  Damaged item  "}).reason == "Damaged item"


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RefundRequest.model_validate({**VALID, "approved_by": "agent"})


def test_missing_fields_are_rejected():
    with pytest.raises(ValidationError):
        RefundRequest.model_validate({"customer_id": "CUST-00001"})
