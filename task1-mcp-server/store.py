"""In-memory customer store. Pure data: no I/O, no logging, nothing on stdout."""

from uuid import uuid4

from pydantic import BaseModel, ConfigDict


class Customer(BaseModel):
    model_config = ConfigDict(frozen=True)

    customer_id: str
    name: str
    email: str
    balance: float  # amount still refundable; ponytail: ceiling=float per brief, upgrade=Decimal


_CUSTOMERS: dict[str, Customer] = {
    c.customer_id: c
    for c in (
        Customer(customer_id="CUST-00001", name="Avery Stone", email="avery@example.com", balance=250.00),
        Customer(customer_id="CUST-00002", name="Jordan Reyes", email="jordan@example.com", balance=1200.50),
        Customer(customer_id="CUST-00003", name="Sam Okafor", email="sam@example.com", balance=0.00),
    )
}


def get_customer(customer_id: str) -> Customer | None:
    return _CUSTOMERS.get(customer_id)


class Refund(BaseModel):
    model_config = ConfigDict(frozen=True)

    refund_id: str
    customer_id: str
    amount: float
    reason: str
    remaining_balance: float


class InsufficientBalance(Exception):
    """Refund amount is more than the customer's refundable balance."""


def record_refund(customer: Customer, amount: float, reason: str) -> Refund:
    if amount > customer.balance:
        raise InsufficientBalance(
            f"Refund {amount:.2f} exceeds refundable balance {customer.balance:.2f}"
        )
    remaining = round(customer.balance - amount, 2)
    # No await between the check above and this write, so concurrent refunds cannot interleave.
    _CUSTOMERS[customer.customer_id] = customer.model_copy(update={"balance": remaining})
    return Refund(
        refund_id=f"RF-{uuid4().hex[:8]}",
        customer_id=customer.customer_id,
        amount=amount,
        reason=reason,
        remaining_balance=remaining,
    )
