"""Record / replay of real Claude responses for prompt-quality tests.

Replay (default): responses come from tests/llm_fixtures/<case>.json, so the API is never
called. Record (TRAVELRAG_LLM_RECORD=1): the real API is called and the fixture rewritten.

Each recorded call stores a hash of (task, system prompt, user prompt, output schema). If the
production code now builds a different prompt, replay fails and asks for a re-recording
instead of silently reusing an answer to a different question. UUIDs in prompts (ids generated
per run) are replaced by <id1>, <id2>... in order of appearance, and the same placeholders in
recorded outputs are mapped back to the current run's ids.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from travelrag.llm.ports import StructuredLLM, T

FIXTURES = Path(__file__).resolve().parents[2] / "llm_fixtures"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RECORD_HINT = (
    "Re-record with: TRAVELRAG_LLM_RECORD=1 TRAVELRAG_TEST_DATABASE_URL=... "
    "uv run pytest tests/integration/llm  (calls the Claude API)"
)


class FixtureError(AssertionError):
    pass


def _normalize(prompt: str) -> tuple[str, dict[str, str]]:
    """Replace UUIDs by stable placeholders; return (text, uuid -> placeholder)."""
    mapping: dict[str, str] = {}

    def sub(match: re.Match[str]) -> str:
        value = match.group(0)
        mapping.setdefault(value, f"<id{len(mapping) + 1}>")
        return mapping[value]

    return _UUID.sub(sub, prompt), mapping


def _replace_strings(data: Any, fn: Any) -> Any:
    if isinstance(data, str):
        return fn(data)
    if isinstance(data, list):
        return [_replace_strings(v, fn) for v in data]
    if isinstance(data, dict):
        return {k: _replace_strings(v, fn) for k, v in data.items()}
    return data


def _key(task: str, system: str, prompt: str, output: type[BaseModel]) -> str:
    schema = json.dumps(output.model_json_schema(), sort_keys=True)
    return hashlib.sha256("\x00".join([task, system, prompt, schema]).encode()).hexdigest()[:16]


class CassetteLLM:
    """StructuredLLM that records real calls or replays recorded ones, in call order."""

    def __init__(self, case: str, real: StructuredLLM | None) -> None:
        self.case = case
        self.real = real  # None -> replay
        self.path = FIXTURES / f"{case}.json"
        self.calls: list[dict[str, Any]] = []
        self._recorded: list[dict[str, Any]] = []
        if real is None:
            if not self.path.exists():
                raise FixtureError(f"no recorded responses for '{case}'. {RECORD_HINT}")
            self._recorded = json.loads(self.path.read_text())["calls"]

    @property
    def recording(self) -> bool:
        return self.real is not None

    async def generate(
        self,
        *,
        task: str,
        system: str,
        prompt: str,
        output: type[T],
        effort: Any = "medium",
        max_tokens: int = 8000,
    ) -> T:
        normalized, ids = _normalize(prompt)
        key = _key(task, system, normalized, output)
        index = len(self.calls)
        if self.real is not None:
            result = await self.real.generate(
                task=task, system=system, prompt=prompt, output=output,
                effort=effort, max_tokens=max_tokens,
            )  # fmt: skip
            to_placeholder = lambda s: _UUID.sub(lambda m: ids.get(m.group(0), m.group(0)), s)  # noqa: E731
            data = _replace_strings(result.model_dump(mode="json"), to_placeholder)
            self.calls.append({"task": task, "key": key, "prompt": normalized, "output": data})
            return result

        if index >= len(self._recorded):
            raise FixtureError(
                f"'{self.case}' made an unrecorded call #{index + 1} ({task}). {RECORD_HINT}"
            )
        recorded = self._recorded[index]
        if recorded["task"] != task or recorded["key"] != key:
            raise FixtureError(
                f"'{self.case}' call #{index + 1} ({task}): the prompt changed since it was "
                f"recorded, so the recorded answer no longer applies. {RECORD_HINT}"
            )
        back = {placeholder: uuid for uuid, placeholder in ids.items()}
        data = _replace_strings(
            recorded["output"],
            lambda s: re.sub(r"<id\d+>", lambda m: back.get(m.group(0), m.group(0)), s),
        )
        self.calls.append(recorded)
        return output.model_validate(data)

    def save(self, model: str) -> None:
        if self.real is None:
            return
        FIXTURES.mkdir(parents=True, exist_ok=True)
        payload = {
            "case": self.case,
            "model": model,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "calls": self.calls,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def outputs(self, task: str) -> list[dict[str, Any]]:
        return [c["output"] for c in self.calls if c["task"] == task]
