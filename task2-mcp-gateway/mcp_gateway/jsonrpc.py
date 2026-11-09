"""The JSON-RPC 2.0 request envelope, validated at the trust boundary."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    jsonrpc: Literal["2.0"]
    method: StrictStr
    id: StrictInt | StrictStr | None = None  # absent = notification
    params: dict[str, Any] | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_not_null(cls, value: object) -> object:
        # MCP 2026-07-28: "Unlike base JSON-RPC, the ID MUST NOT be null."
        if value is None:
            raise ValueError("id must not be null")
        return value

    @property
    def is_notification(self) -> bool:
        return self.id is None
