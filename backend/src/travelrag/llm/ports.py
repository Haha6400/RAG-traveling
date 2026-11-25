from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Effort = Literal["low", "medium", "high"]


class LLMUnavailableError(Exception):
    """No LLM is configured (e.g. missing API key) or the provider cannot be reached."""


class LLMRefusalError(Exception):
    """The model declined the request."""


class StructuredLLM(Protocol):
    async def generate(
        self,
        *,
        task: str,
        system: str,
        prompt: str,
        output: type[T],
        effort: Effort = "medium",
        max_tokens: int = 8000,
    ) -> T:
        """Return a validated instance of `output`. `task` names the call site (for logs and
        test fakes)."""
        ...
