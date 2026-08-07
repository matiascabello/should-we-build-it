import json
import re
from openai import OpenAI
from langsmith.wrappers import wrap_openai
from langgraph.config import get_stream_writer
from graph.state import DebateState, Turn, ToolCall
from graph.tools import TOOL_SCHEMAS, TOOL_DISPATCH
from graph.state import Flag, DecisionMemo
from pydantic import BaseModel

client = wrap_openai(OpenAI())
MODEL = "gpt-5.6-luna"
MAX_TOOL_CALLS = 3   # guardrail: cap evidence-gathering per turn
# Was 2. With 2, using web_search meant giving up an internal-doc call —
# for an internal-product question, internal search always wins that
# tradeoff, which is why web_search went unused across every trace tested
# (gpt-4o and every gpt-5.6-luna reasoning tier). 3 lets a debater pull
# internal grounding *and* external evidence in the same turn instead of
# choosing between them.

# Chat Completions rejects function tools + reasoning_effort together for
# this model ("use /v1/responses or set reasoning_effort to 'none'"), so
# every call goes through the Responses API instead, which supports both
# at once. gpt-5.6-luna's supported tiers are none/low/medium/high/xhigh/max
# ("minimal", offered by other reasoning models, isn't valid here). "low" is
# the lowest tier this model accepts — a trivial tool call used 0 reasoning
# tokens at this level in testing, so cost stays close to reasoning-off while
# still letting the model reason when a step actually calls for it.
REASONING_EFFORT = "medium"


ADVOCATE_SYSTEM = """You are the Advocate in a product decision debate.
Your job: argue the strongest evidence-based case FOR building the feature.
Use BOTH your tools, not just one: internal search for what users and the
PRD actually say about this product, and web search for outside evidence —
how comparable features performed at other companies, adoption or success
benchmarks for similar launches, market sizing — that corroborates the
internal signal instead of resting on internal anecdotes alone. Reach for
web search whenever an internal claim would be more convincing backed by an
external data point, not only when internal search comes up empty. Ground
every claim in something you retrieved. Be persuasive but honest; do not
invent facts. Keep your argument to 3-5 sentences. Respond to the opponent's
prior points when relevant. If you have argued in prior rounds, do NOT repeat your earlier points. Advance the debate: directly rebut your opponent's most recent argument, or introduce evidence you haven't used yet."""

SKEPTIC_SYSTEM = """You are the Skeptic in a product decision debate.
Your job: argue the strongest evidence-based case AGAINST building the feature
(or for descoping/delaying it). Use BOTH your tools, not just one: internal
search for the PRD's own stated risks and costs, and web search for outside
evidence — published failure or backlash stories for comparable AI features,
real-world error/complaint rates, how competitors handled the same tradeoff —
that tests whether an internal assumption actually holds up. Reach for web
search whenever it could surface a risk or precedent the internal docs
wouldn't know to mention, not only when internal search comes up empty.
Ground every claim in something you retrieved. Be rigorous but honest; do not
invent facts. Keep your argument to 3-5 sentences. Respond to the opponent's
prior points when relevant. If you have argued in prior rounds, do NOT repeat your earlier points. Advance the debate: directly rebut your opponent's most recent argument, or introduce evidence you haven't used yet."""


# Occasionally the model emits an unexecuted tool-call attempt as raw text
# instead of a structured function_call item — it lands in output_text
# glued onto the front of the real argument rather than being caught by our
# function_call handling. This has shown up in at least four distinct
# literal shapes across testing ('to=functions.X code{...}', 'to=web.run
# code{...}', a bare 'THOOK}' debris fragment, a '{"q":...}],"..."'
# array-style fragment) — different pseudo tool names, different argument
# keys, different wrapping. Enumerating each new shape as its own regex
# lost that game twice in a row, so detection is generalized instead: a
# debate argument is plain English prose and never legitimately contains a
# literal '{' or '}' — every leaked variant seen so far did, near the start
# of the text. That single signal is far more robust than matching specific
# prefixes. Since it ships straight to the transcript/UI verbatim if
# unhandled: retry once, and if it recurs, cut through the leaked syntax so
# garbled text can never reach a user regardless of what shape it takes.
_LEAK_SCAN_WINDOW = 200   # only look at the head — a legitimate argument
                          # quoting a brace deep in its text shouldn't count
MAX_LEAK_RETRIES = 1


def _looks_like_leaked_tool_call(text: str) -> bool:
    head = text[:_LEAK_SCAN_WINDOW]
    return "{" in head or "}" in head


def _strip_leaked_tool_call_syntax(text: str) -> str:
    """Cut everything through the last brace in the leaked region, then
    trim any leftover leading debris (stray punctuation/tokens) that isn't
    the start of a real sentence."""
    head = text[:_LEAK_SCAN_WINDOW]
    cut = max(head.rfind("{"), head.rfind("}"))
    remainder = text[cut + 1:] if cut != -1 else text
    # Drop any leading run of characters that isn't a letter or an opening
    # quote — covers stray commas/brackets/control-token fragments left
    # over from the cut without being able to name every shape up front.
    remainder = re.sub(r'\A[^A-Za-z"“]+', '', remainder)
    return remainder.strip()


def _final_answer_text(response) -> str:
    """response.output_text concatenates every message-type output item's
    text with no separator — including 'commentary'-phase messages (the
    model narrating its own search/reasoning process out loud), gluing that
    narration onto the front of the real argument with no space between
    them. Pull text only from 'final_answer'-phase content; an absent phase
    is treated as final since not every model sets it."""
    parts = [
        part.text
        for item in response.output
        if item.type == "message" and getattr(item, "phase", None) != "commentary"
        for part in item.content
        if getattr(part, "type", None) == "output_text"
    ]
    return "\n\n".join(parts).strip()


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

    input_list = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"FEATURE DECISION: {state['question']}\n\n"
            f"DEBATE SO FAR:\n{_render_transcript(state['transcript'])}\n\n"
            f"Gather evidence with your tools, then make your argument."
        )},
    ]

    collected_tool_calls: list[ToolCall] = []
    tool_calls_made = 0
    leak_retries_used = 0
    # Pushes each tool call to the UI the moment it happens, via LangGraph's
    # custom stream channel (stream_mode="custom") — separate from the Turn
    # this function returns at the end, which server/app.py still emits as
    # the "turn" SSE event once the argument is ready.
    stream_writer = get_stream_writer()

    while True:
        # Only offer tools while under the cap; once capped, force a final answer.
        # reasoning is applied on every call (not just while tools are offered)
        # so behavior is uniform regardless of how many tool calls the model
        # happens to make — see REASONING_EFFORT above for why this goes
        # through /v1/responses rather than Chat Completions.
        kwargs = {
            "model": MODEL, "input": input_list,
            "reasoning": {"effort": REASONING_EFFORT},
        }
        if tool_calls_made < MAX_TOOL_CALLS:
            kwargs["tools"] = TOOL_SCHEMAS
            kwargs["tool_choice"] = "auto"
            kwargs["parallel_tool_calls"] = False   # one search per turn

        response = client.responses.create(
            **kwargs,
            langsmith_extra={"name": f"{role}_reasoning"},
            )

        function_calls = [item for item in response.output if item.type == "function_call"]

        # No tool calls requested -> this is the final argument.
        if not function_calls:
            argument = _final_answer_text(response)
            if _looks_like_leaked_tool_call(argument):
                if leak_retries_used < MAX_LEAK_RETRIES:
                    leak_retries_used += 1
                    continue   # re-issue the same request; input_list is unchanged
                print(
                    f"[warn] {role} r{state['round']}: leaked tool-call "
                    f"syntax survived a retry, stripping before use"
                )
                argument = _strip_leaked_tool_call_syntax(argument)
            break

        # Otherwise, echo the assistant's output items back, then execute.
        input_list += response.output
        for fc in function_calls:
            fn_name = fc.name
            args = json.loads(fc.arguments)
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
            input_list.append({
                "type": "function_call_output",
                "call_id": fc.call_id,
                "output": result_text,
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
actually retrieved (their tool calls). Your job is to catch claims that
misrepresent or outrun that evidence — not to grade writing quality.

First, extract every checkable factual claim from the round: a specific
number, a quoted or paraphrased user statement, a named risk or metric from
the source documents, or an assertion about what the evidence shows. Split
compound sentences into separate claims — if one sentence bundles three
facts, assess each one on its own, so a single wrong fact can't hide behind
two correct ones.

Do NOT extract the debater's overall recommendation, strategy, or proposed
next step (e.g. "we should launch narrowly," "this justifies a pilot") as a
claim to assess — these are not factual assertions, and have nothing to be
supported or unsupported against. Leave them out of the list entirely.

For each extracted claim, check whether the retrieved evidence actually
states it or directly entails it. Mark it unsupported if it:
- states a number, quote, or fact that is not present in the evidence shown
- asserts what a metric or pilot result would prove or "validate" beyond
  what that metric actually measures (e.g. "X% of users trying it would
  validate demand" is overreach unless the evidence itself makes that
  connection — a trial-rate metric proves trial, not demand or retention)
- generalizes from a small evidence sample to a broader population the
  evidence doesn't cover (e.g. four quotes -> "most users report...")

Do not flag genuine opinions, predictions framed as opinions, or reasonable
inferences that stay within what the evidence supports. Be strict about
invented specifics and about claims that quietly convert "the evidence
exists" into "the evidence proves" — lenient about honestly-hedged judgment
calls.

Return a list of the claims you assessed, each marked supported or not."""


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

    response = client.responses.parse(
        model=MODEL,
        reasoning={"effort": REASONING_EFFORT},  # see _run_debater — uniform across all MODEL calls
        input=[
            {"role": "system", "content": FACTCHECK_SYSTEM},
            {"role": "user", "content": (
                f"FEATURE: {state['question']}\n\n"
                f"THIS ROUND:\n{_render_round_for_check(state)}"
            )},
        ],
        text_format=FlagList,
    )

    flags = response.output_parsed.flags
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
    response = client.responses.parse(
        model=MODEL,
        reasoning={"effort": REASONING_EFFORT},  # see _run_debater — uniform across all MODEL calls
        input=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": (
                f"FEATURE DECISION: {state['question']}\n\n"
                f"FULL DEBATE:\n{_render_full_debate(state)}\n\n"
                f"Render your decision."
            )},
        ],
        text_format=DecisionMemo,
    )
    return {"verdict": response.output_parsed}