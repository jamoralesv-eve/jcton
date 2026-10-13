import warnings
from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.agents import SequentialAgent
from google.genai import types
from pydantic import BaseModel
from pydantic import Field

from pr_gate.prompts import COMPARE_PROMPT
from pr_gate.prompts import REBUILD_PROMPT

MODEL = "gemini-2.5-flash"


class Verdict(BaseModel):
    overall_distance: float = Field(
        ge=0,
        le=1,
        description="0 means the change matches the ticket. 1 means it does not.",
    )
    missing_in_pr: list[str] = Field(
        description="Requirements from the ticket that the change does not show.",
    )
    unrequested_changes: list[str] = Field(
        description="New behavior the ticket does not ask for.",
    )
    justification: str
    confidence: Literal["low", "medium", "high"]


def _model_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(temperature=0)


# original_ticket is in session state, but this instruction never names it.
rebuild_agent = LlmAgent(
    name="ticket_rebuilder",
    model=MODEL,
    instruction=REBUILD_PROMPT + "\n\n<pr_diff>\n{pr_diff}\n</pr_diff>",
    include_contents="none",
    output_key="reconstructed_ticket",
    generate_content_config=_model_config(),
)

compare_agent = LlmAgent(
    name="semantic_comparator",
    model=MODEL,
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
