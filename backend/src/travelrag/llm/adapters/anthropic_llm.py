import logging
import time

import anthropic

from travelrag.llm.ports import Effort, LLMRefusalError, LLMUnavailableError, T

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    """Claude via the Messages API with structured outputs (Pydantic models).

    Server-side refusal fallbacks are enabled ("default" routing), so a declined request is
    re-run on a fallback model inside the same call.
    """

    def __init__(self, api_key: str | None, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        self._model = model

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
        if self._client is None:
            raise LLMUnavailableError("TRAVELRAG_ANTHROPIC_API_KEY is not set")
        started = time.monotonic()
        try:
            response = await self._client.beta.messages.parse(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=output,
                output_config={"effort": effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMUnavailableError(f"Anthropic API rejected the credentials: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError(f"Cannot reach the Anthropic API: {exc}") from exc

        logger.info(
            "llm task=%s model=%s stop=%s in=%s out=%s %.1fs",
            task,
            response.model,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
            time.monotonic() - started,
        )
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMRefusalError(f"model declined ({category})")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMRefusalError(f"no structured output (stop_reason={response.stop_reason})")
        return parsed
