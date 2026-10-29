import os
import warnings
from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.agents import SequentialAgent
from google.adk.models.lite_llm import LiteLlm
from google.genai import types
from pydantic import BaseModel
from pydantic import Field

from pr_gate.errors import GateError
from pr_gate.prompts import COMPARE_PROMPT
from pr_gate.prompts import REBUILD_PROMPT

# A bare "claude-*" name resolves to Vertex. The anthropic/ prefix uses ANTHROPIC_API_KEY.
DEFAULT_MODEL = "anthropic/claude-sonnet-4-5"
_PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "ollama": "",
    "ollama_chat": "",
}


class Alignment(BaseModel):
    a: str
    b: str
    relation: Literal["equivalent", "partial", "contradictory", "no correspondence"]


class Dimensions(BaseModel):
    topic: int = Field(ge=0, le=10)
    factual: int = Field(ge=0, le=10)
    conclusions: int = Field(ge=0, le=10)
    detail: int = Field(ge=0, le=10)
    tone: int = Field(ge=0, le=10)


class Verdict(BaseModel):
    alignment: list[Alignment]
    dimensions: Dimensions
    overall_distance: float = Field(ge=0, le=1)
    justification: str
    confidence: Literal["low", "medium", "high"]


def model_name() -> str:
    chosen = os.environ.get("PR_GATE_MODEL", "").strip()
    if chosen:
        return chosen
    return DEFAULT_MODEL


def required_api_key(name: str) -> str:
    provider = name.split("/", 1)[0]
    if provider not in _PROVIDER_KEYS:
        raise GateError(f"Proveedor no soportado: {provider}")
    return _PROVIDER_KEYS[provider]


def gate_model() -> LiteLlm:
    base = os.environ.get("PR_GATE_API_BASE", "").strip()
    if base:
        return LiteLlm(model=model_name(), api_base=base)
    return LiteLlm(model=model_name())


def _model_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(temperature=0)


# original_ticket is in session state, but this instruction never names it.
rebuild_agent = LlmAgent(
    name="ticket_rebuilder",
    model=gate_model(),
    instruction=REBUILD_PROMPT + "\n\n<pr_diff>\n{pr_diff}\n</pr_diff>",
    include_contents="none",
    output_key="reconstructed_ticket",
    generate_content_config=_model_config(),
)

compare_agent = LlmAgent(
    name="semantic_comparator",
    model=gate_model(),
    instruction=(
        COMPARE_PROMPT
        + "\n\n<document_a>\n{original_ticket}\n</document_a>"
        + "\n<document_b>\n{reconstructed_ticket}\n</document_b>"
    ),
    include_contents="none",
    output_schema=Verdict,
    output_key="verdict",
    generate_content_config=_model_config(),
)

# ADK 2.12 still runs SequentialAgent. Workflow is not a drop-in yet.
warnings.filterwarnings(
    "ignore",
    message="SequentialAgent is deprecated",
    category=DeprecationWarning,
)

root_agent = SequentialAgent(
    name="pr_ticket_gate",
    sub_agents=[rebuild_agent, compare_agent],
)
