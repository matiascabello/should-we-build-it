import json
from openai import OpenAI
from langsmith.wrappers import wrap_openai
from langgraph.config import get_stream_writer
from graph.state import DebateState, Turn, ToolCall
from graph.tools import TOOL_SCHEMAS, TOOL_DISPATCH
from graph.state import Flag, DecisionMemo
from pydantic import BaseModel

client = wrap_openai(OpenAI())
MODEL = "gpt-4o"
MAX_TOOL_CALLS = 2   # guardrail: cap evidence-gathering per turn


ADVOCATE_SYSTEM = """You are the Advocate in a product decision debate.
Your job: argue the strongest evidence-based case FOR building the feature.
Use the tools to gather real evidence — internal user feedback, the PRD, or
web/market data — before you argue. Ground every claim in something you
retrieved. Be persuasive but honest; do not invent facts. Keep your argument
to 3-5 sentences. Respond to the opponent's prior points when relevant. If you have argued in prior rounds, do NOT repeat your earlier points. Advance the debate: directly rebut your opponent's most recent argument, or introduce evidence you haven't used yet."""

SKEPTIC_SYSTEM = """You are the Skeptic in a product decision debate.
Your job: argue the strongest evidence-based case AGAINST building the feature
(or for descoping/delaying it). Use the tools to gather real evidence —
internal user feedback, the PRD, costs, or web/market data — before you argue.
Ground every claim in something you retrieved. Be rigorous but honest; do not
invent facts. Keep your argument to 3-5 sentences. Respond to the opponent's
prior points when relevant. If you have argued in prior rounds, do NOT repeat your earlier points. Advance the debate: directly rebut your opponent's most recent argument, or introduce evidence you haven't used yet."""


def _render_transcript(transcript: list[Turn]) -> str:
    if not transcript:
        return "(no arguments yet — you are opening the debate)"
    lines = []
    for t in transcript:
        lines.append(f"[Round {t.round}] {t.role.upper()}: {t.argument}")
    return "\n".join(lines)


def _run_debater(state: DebateState, role: str, system_prompt: str) -> dict:
    """Shared agent loop for both debaters. Returns a partial state
    with one new Turn appended to the transcript."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"FEATURE DECISION: {state['question']}\n\n"
            f"DEBATE SO FAR:\n{_render_transcript(state['transcript'])}\n\n"
            f"Gather evidence with your tools, then make your argument."
        )},
    ]

    collected_tool_calls: list[ToolCall] = []
    tool_calls_made = 0
    # Pushes each tool call to the UI the moment it happens, via LangGraph's
    # custom stream channel (stream_mode="custom") — separate from the Turn
    # this function returns at the end, which server/app.py still emits as
    # the "turn" SSE event once the argument is ready.
    stream_writer = get_stream_writer()

    while True:
        # Only offer tools while under the cap; once capped, force a final answer.
        kwargs = {"model": MODEL, "messages": messages}
        if tool_calls_made < MAX_TOOL_CALLS:
            kwargs["tools"] = TOOL_SCHEMAS
            kwargs["tool_choice"] = "auto"
            kwargs["parallel_tool_calls"] = False   # one search per turn

        response = client.chat.completions.create(
            **kwargs,
            langsmith_extra={"name": f"{role}_reasoning"},
            )
        msg = response.choices[0].message

        # No tool calls requested -> this is the final argument.
        if not msg.tool_calls:
            argument = msg.content.strip()
            break

        # Otherwise, append the assistant's tool-request message, then execute.
        messages.append(msg)
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            args = json.loads(tc.function.arguments)
            query = args.get("query", "")

            evidence = TOOL_DISPATCH[fn_name](query)
            tool_calls_made += 1

            # Record for the transcript (what the UI will show), and push
            # each one live as it's retrieved (before the argument exists).
            for ev in evidence:
                tc_obj = ToolCall(
                    tool=fn_name, query=query,
                    source=ev.source, snippet=ev.content[:500],
                )
                collected_tool_calls.append(tc_obj)
                stream_writer({
                    "type": "tool_call",
                    "role": role,
                    "round": state["round"],
                    "tool": tc_obj.tool,
                    "query": tc_obj.query,
                    "source": tc_obj.source,
                    "snippet": tc_obj.snippet,
                })

            # Feed the result back to the model.
            result_text = "\n\n".join(
                f"[{ev.source}] {ev.content}" for ev in evidence
            ) or "(no results)"
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_text,
            })

    turn = Turn(
        round=state["round"],
        role=role,
        argument=argument,
        tool_calls=collected_tool_calls,
    )
    return {"transcript": [turn]}


def advocate_node(state: DebateState) -> dict:
    return _run_debater(state, "advocate", ADVOCATE_SYSTEM)


def skeptic_node(state: DebateState) -> dict:
    return _run_debater(state, "skeptic", SKEPTIC_SYSTEM)

# ---------------------------------------------------------------
# Fact-checker: flags claims not grounded in retrieved evidence
# ---------------------------------------------------------------

FACTCHECK_SYSTEM = """You are a fact-checker in a product decision debate.
You are given the arguments made THIS round and the evidence each debater
actually retrieved (their tool calls). For each significant factual claim a
debater makes, decide whether it is supported by the evidence they gathered
or by general common knowledge.

Flag a claim as unsupported if it asserts a specific fact (a number, a user
sentiment, a market figure, a competitor outcome) that is NOT backed by the
evidence shown. Do not flag opinions, predictions, or reasonable inferences.
Be strict about invented specifics, lenient about judgment calls.

Return a list of claims you assessed, each marked supported or not."""


def _render_round_for_check(state: DebateState) -> str:
    """Only the turns from the current round, with their evidence."""
    current = [t for t in state["transcript"] if t.round == state["round"]]
    blocks = []
    for t in current:
        ev = "\n".join(
            f"    - [{tc.tool}] {tc.source}: {tc.snippet}"
            for tc in t.tool_calls
        ) or "    (no evidence retrieved)"
        blocks.append(
            f"{t.role.upper()} argued: {t.argument}\n"
            f"  Evidence they retrieved:\n{ev}"
        )
    return "\n\n".join(blocks)


def factcheck_node(state: DebateState) -> dict:
    """Assess this round's claims. Returns flagged_claims (appended)."""

    class FlagList(BaseModel):
        flags: list[Flag]

    completion = client.beta.chat.completions.parse(
        model=MODEL,
        messages=[
            {"role": "system", "content": FACTCHECK_SYSTEM},
            {"role": "user", "content": (
                f"FEATURE: {state['question']}\n\n"
                f"THIS ROUND:\n{_render_round_for_check(state)}"
            )},
        ],
        response_format=FlagList,
    )

    flags = completion.choices[0].message.parsed.flags
    # Stamp the round number (the model doesn't need to set it).
    for f in flags:
        f.round = state["round"]
    return {"flagged_claims": flags}


# ---------------------------------------------------------------
# Judge: emits the final structured DecisionMemo (runs once)
# ---------------------------------------------------------------

JUDGE_SYSTEM = """You are the Judge in a product decision debate. You have the
full transcript of arguments from both sides, plus a fact-checker's flags on
unsupported claims.

Weigh the arguments by the quality of their evidence. Discount claims the
fact-checker flagged as unsupported. Produce a clear, honest recommendation.

Your recommendation must be one of:
- "build": strong evidence-based case to build as proposed
- "dont_build": the case against outweighs the case for
- "build_descoped": build, but narrow the scope or delay pending specific info

Set confidence honestly — if the debate left real uncertainty, say so and keep
confidence moderate. In "what_would_change_my_mind", name the SPECIFIC missing
evidence that would flip your recommendation. In "open_questions", list
concrete things a PM should resolve before committing."""


def _render_full_debate(state: DebateState) -> str:
    lines = []
    for t in state["transcript"]:
        lines.append(f"[Round {t.round}] {t.role.upper()}: {t.argument}")
    flags = [f for f in state["flagged_claims"] if not f.supported]
    if flags:
        lines.append("\nFACT-CHECK FLAGS (unsupported claims):")
        for f in flags:
            lines.append(f"  - {f.role} (R{f.round}): {f.claim} — {f.note}")
    return "\n".join(lines)


def judge_node(state: DebateState) -> dict:
    """Consume the full debate, emit a validated DecisionMemo."""
    completion = client.beta.chat.completions.parse(
        model=MODEL,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": (
                f"FEATURE DECISION: {state['question']}\n\n"
                f"FULL DEBATE:\n{_render_full_debate(state)}\n\n"
                f"Render your decision."
            )},
        ],
        response_format=DecisionMemo,
    )
    return {"verdict": completion.choices[0].message.parsed}