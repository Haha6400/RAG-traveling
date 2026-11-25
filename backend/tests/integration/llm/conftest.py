import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from tests.integration.llm.cassette import CassetteLLM
from travelrag.core.config import get_settings
from travelrag.llm.adapters.anthropic_llm import AnthropicLLM

RECORD = os.environ.get("TRAVELRAG_LLM_RECORD") == "1"
ROOT_ENV = Path(__file__).resolve().parents[4] / ".env"


def _api_key() -> str:
    key = os.environ.get("TRAVELRAG_ANTHROPIC_API_KEY") or get_settings().anthropic_api_key
    if not key and ROOT_ENV.exists():  # the key docker compose uses
        for line in ROOT_ENV.read_text().splitlines():
            if line.startswith("TRAVELRAG_ANTHROPIC_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        pytest.fail("TRAVELRAG_LLM_RECORD=1 needs TRAVELRAG_ANTHROPIC_API_KEY (env or repo .env)")
    return key


@pytest.fixture
async def cassette(request: pytest.FixtureRequest) -> AsyncIterator[Callable[[str], CassetteLLM]]:
    """`cassette("case-name")` -> an LLM that replays (or, when recording, records) that case."""
    made: list[CassetteLLM] = []
    real = AnthropicLLM(_api_key(), get_settings().llm_model) if RECORD else None

    def make(case: str) -> CassetteLLM:
        llm = CassetteLLM(case, real)
        made.append(llm)
        return llm

    yield make
    for llm in made:  # saved even when an assertion failed: the recording is the evidence
        llm.save(get_settings().llm_model)
