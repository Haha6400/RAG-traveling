from functools import cache

from travelrag.core.config import get_settings
from travelrag.llm.adapters.anthropic_llm import AnthropicLLM
from travelrag.llm.ports import StructuredLLM


@cache
def get_llm() -> StructuredLLM:
    settings = get_settings()
    return AnthropicLLM(settings.anthropic_api_key, settings.llm_model)
