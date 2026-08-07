from typing import Annotated, Literal, Optional
from typing_extensions import TypedDict
from operator import add
from pydantic import BaseModel, Field


# --- Artifacts that get appended to the transcript ---

class ToolCall(BaseModel):
    """A single evidence-gathering action a debater took."""
    tool: str            # "search_internal_context" | "web_search"
    query: str
    source: str          # where the result came from (doc name / URL / title)
    snippet: str         # the retrieved content, trimmed
    url: str = ""        # populated for web results (from Evidence.url), empty for internal


class Turn(BaseModel):
    """One debater's contribution in one round."""
    round: int
    role: Literal["advocate", "skeptic"]
    argument: str
    tool_calls: list[ToolCall] = Field(default_factory=list)


class Flag(BaseModel):
    """Fact-checker's assessment of a claim."""
    round: int
    role: Literal["advocate", "skeptic"]
    claim: str
    supported: bool
    note: str


class DecisionMemo(BaseModel):
    """The judge's final structured output."""
    recommendation: Literal["build", "dont_build", "build_descoped"]
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_rationale: str
    key_tradeoffs: list[str]
    what_would_change_my_mind: list[str]
    open_questions: list[str]


# --- The shared state that flows through the graph ---

class DebateState(TypedDict):
    question: str
    max_rounds: int
    round: int
    transcript: Annotated[list[Turn], add]
    flagged_claims: Annotated[list[Flag], add]
    verdict: Optional[DecisionMemo]