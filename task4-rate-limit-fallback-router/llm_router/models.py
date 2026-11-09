from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(frozen=True, extra="forbid", strict=True)


class CompletionRequest(BaseModel):
    """Client body. Validated once at the trust boundary (app.py)."""

    model_config = _STRICT

    prompt: Annotated[str, Field(min_length=1, max_length=200_000)]
    max_tokens: Annotated[int, Field(ge=1, le=32_000)] = 256

    def estimated_tokens(self) -> int:
        # shortcut: ceil(chars / 4) for the prompt, upgrade to the provider's tokenizer.
        # Output is reserved at max_tokens (worst case); reconcile() corrects it after the call.
        return -(-len(self.prompt) // 4) + self.max_tokens


class Usage(BaseModel):
    model_config = _STRICT
    total_tokens: Annotated[int, Field(ge=0)]


class CompletionResponse(BaseModel):
    model_config = _STRICT
    text: str
    provider: Literal["primary", "secondary"]  # a role, never a hostname or vendor
    usage: Usage
