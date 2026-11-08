"""Strict input models for the two tools.

These types define the -32602 contract, and the same types in the tool
signatures make tools/list advertise the constraints in inputSchema.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# [0-9], not \d: \d also matches non-ASCII digits such as "٠".
CustomerId = Annotated[
    str,
    Field(
        pattern=r"^CUST-[0-9]{5}$",
        description="Customer id in the format CUST-XXXXX, where each X is a digit 0-9.",
        examples=["CUST-00001"],
    ),
]

# ponytail: ceiling=flat 1,000,000 cap, upgrade=per-customer or per-agent refund limits
Amount = Annotated[
    float,
    Field(gt=0, le=1_000_000, allow_inf_nan=False, description="Refund amount. Must be greater than 0."),
]

Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=10, max_length=500),
    Field(description="Why the refund is issued. At least 10 characters after trimming spaces."),
]


class GetCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    customer_id: CustomerId


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    customer_id: CustomerId
    amount: Amount
    reason: Reason
