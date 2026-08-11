from graph.state import Turn, ToolCall, Flag, DecisionMemo, ScopeCheck, DebateState

# Build a turn with a tool call — mimics what a debater node will emit
turn = Turn(
    round=1,
    role="advocate",
    argument="Users are asking for this repeatedly.",
    tool_calls=[
        ToolCall(
            tool="search_internal_context",
            query="user demand for summaries",
            source="feedback.md",
            snippet="I'd pay extra for auto-summaries.",
        )
    ],
)
print("Turn OK:", turn.role, "-", turn.argument)

# The memo — the one that MUST validate cleanly, since the judge emits JSON into it
memo = DecisionMemo(
    recommendation="build_descoped",
    confidence=0.65,
    confidence_rationale="Demand is real but accuracy risk is unresolved.",
    key_tradeoffs=["High user demand", "Accuracy concerns on long notes"],
    what_would_change_my_mind=["Evidence that summary accuracy exceeds 90%"],
    open_questions=["What's the compute cost per summary?"],
)
print("Memo OK:", memo.recommendation, "@", memo.confidence)

# The scope gate's verdict — must also validate cleanly, since it's JSON the
# gate node emits before any debater runs
check = ScopeCheck(in_scope=False, reason="This is a request to debug code, not a build decision.")
print("ScopeCheck OK:", check.in_scope, "-", check.reason)

# Construct a state dict by hand — this is what flows through the graph
state: DebateState = {
    "question": "Should we add AI summaries to our note app?",
    "max_rounds": 3,
    "round": 0,
    "transcript": [turn],
    "flagged_claims": [],
    "verdict": None,
    "rejection": None,
}
print("State OK:", state["question"], "| turns:", len(state["transcript"]))