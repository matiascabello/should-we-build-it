# Chasing a cheaper model without losing the debate: a quality investigation

**TL;DR:** We swapped the debate engine's model from `gpt-4o` to a cheaper
model (`gpt-5.6-luna`) to cut inference cost. The swap broke in two stages —
first an API-level error, then a silent quality regression that took several
rounds of trace comparison to even notice. Fixing it properly meant migrating
from the Chat Completions API to the Responses API, tuning reasoning effort,
rewriting the fact-checker's prompt, and finally catching a live model
reliability bug that was leaking raw tool-call syntax into user-visible
arguments. This doc walks through the investigation in the order it actually
happened, because the *process* — how we caught each regression — is as
useful as the fixes themselves.

---

## The setup

This app runs an Advocate and a Skeptic through a multi-round debate over a
product decision, with a fact-checker grading each round's claims against the
evidence the debaters actually retrieved, and a judge rendering a final
`DecisionMemo`. All the LLM calls in `graph/nodes.py` shared one `MODEL`
constant. Changing it should have been a one-line edit.

## Stage 1: the model swap breaks immediately

Switching `MODEL` to `gpt-5.6-luna` failed on the very first debater turn:

```
Function tools with reasoning_effort are not supported for gpt-5.6-luna
in /v1/chat/completions. To use function tools, use /v1/responses or
set reasoning_effort to 'none'.
```

`gpt-5.6-luna` is a reasoning-tier model that defaults reasoning on, and the
Chat Completions API refuses to combine that with function tools. The fast
fix was the one the error message offered: force `reasoning_effort="none"`
on every call. This unblocked the app immediately — and, as a bonus, aligned
with the actual goal (reasoning tokens are billed as output tokens, so
disabling reasoning compounds the savings from the cheaper base model).

It also quietly threw away the model's reasoning on every call that offers
tools — which, in this codebase, is every debater turn.

## Stage 2: a baseline makes the regression visible

A single trace only tells you what happened, not whether it's *worse* than
before. The turning point in this investigation was pulling a LangSmith trace
of the same question (`"Should we add AI-generated summaries to our
note-taking app?"`) run the day before on `gpt-4o`, and diffing it against
the `reasoning_effort="none"` run claim-by-claim.

Two regressions only showed up in that diff:

**1. The debate stopped advancing.** Both debaters' system prompts
explicitly say *"If you have argued in prior rounds, do NOT repeat your
earlier points."* On `gpt-4o`, the skeptic genuinely escalated each round —
round 1 pulled the accuracy/hallucination risk from the PRD, round 2 pulled
the success-metrics/churn angle, round 3 pulled the cost figures. On
`gpt-5.6-luna` with reasoning off, all three rounds recycled the same four
data points with only cosmetic rephrasing.

**2. The fact-checker nearly stopped catching anything.**

| Run | Claims assessed | Flagged unsupported | Rate |
|---|---|---|---|
| `gpt-4o` baseline | 15 | 6 | **40%** |
| `gpt-5.6-luna`, `reasoning_effort="none"` | 45 | 1 | **2%** |

The `gpt-4o` fact-checker caught real overreach — an advocate claiming
users wanted summaries for "lectures and research" when the evidence only
said meeting notes; a skeptic asserting churn would "measurably increase"
from a bad thumbs-down rate with no such data. The reasoning-off fact-checker
marked almost everything `supported: true`, including outright speculation,
rationalized with notes like *"this is a judgment call."*

(One thing the baseline diff ruled out: neither run ever called the
`web_search` tool, only `search_internal_context`. That held true across
every configuration tested later too — not a reasoning-effort effect, just
this question being answerable from internal docs within the 2-tool-call
budget.)

## Stage 3: migrating to the Responses API

`reasoning_effort="none"` was a symptom fix, not a real one — the actual
problem was that Chat Completions won't let this model reason *and* use
tools together. The Responses API will. That meant rewriting the shared
debater loop and both structured-output nodes:

```python
# Chat Completions rejects function tools + reasoning_effort together for
# this model, so every call goes through the Responses API instead, which
# supports both at once.
REASONING_EFFORT = "low"

response = client.responses.create(
    model=MODEL,
    input=input_list,
    reasoning={"effort": REASONING_EFFORT},
    tools=TOOL_SCHEMAS,       # only while under the tool-call cap
    tool_choice="auto",
    parallel_tool_calls=False,
)
```

Notable mechanics that differ from Chat Completions: function-tool schemas
are flat (`{"type": "function", "name": ..., "parameters": ...}`, no nested
`"function"` key), tool results round-trip via `function_call_output` items
appended to `input`, and `response.output` items can be fed straight back
into the next call's `input` list. `factcheck_node` and `judge_node` moved
from `client.beta.chat.completions.parse` to `client.responses.parse`
(`text_format=` instead of `response_format=`, `response.output_parsed`
instead of `.choices[0].message.parsed`).

## Stage 4: reasoning effort is not a dial that only goes one direction

With tools and reasoning both available, the next question was *how much*
reasoning to pay for. This model's supported tiers are
`none/low/medium/high/xhigh/max` (no `minimal`, unlike some other reasoning
models — the first attempt at the cheapest non-`none` tier failed with an
unsupported-value error before we found that out).

`low` fully recovered round-over-round progression and got the fact-checker
partway back:

| Run | Claims | Flagged | Rate |
|---|---|---|---|
| `gpt-4o` baseline | 15 | 6 | 40% |
| `reasoning="none"` | 45 | 1 | 2% |
| `reasoning="low"` | 39 | 4 | 10% |

Bumping to `medium` produced the most sophisticated debate content of any
run so far — the skeptic's sharpest point across the whole investigation
showed up here: *"the key question is not whether they will click Summarize,
but whether a summary causes successful retrieval or renewed use."* But the
fact-checker's catch rate went to **zero** — 40 claims assessed, none
flagged, *lower* than the broken `none` run. Going `none → low → medium` is
not a monotonic recovery. More reasoning bought better arguments and,
on this sample, a less discriminating fact-checker — a genuine surprise, and
a reminder that "more reasoning" doesn't uniformly improve every node that
uses it.

## Stage 5: the fact-checker prompt had a real gap, not just noise

Rather than chase the non-monotonic result by guessing at reasoning tiers, we
went looking for *why* the `medium` fact-checker missed things. Lining up
near-identical claims across two runs found a concrete inconsistency:

> **`low` run** (flagged unsupported): *"If the pilot metrics succeed, the
> company will have validated demand in the segment reporting the
> problem."* → *"does not establish that those metrics would validate
> demand."*
>
> **`medium` run** (marked supported): *"...if they do, the team will have
> validated a focused product rather than mass-market demand."* → *"Conditional
> product-decision reasoning, not an unsupported factual assertion."*

Same claim shape — a metric's result being asserted to "validate" more than
it measures — opposite verdict. That's a prompt gap, not a model-capability
ceiling. The original `FACTCHECK_SYSTEM` prompt had three weaknesses: it let
compound sentences bundle multiple facts into one claim (hiding a wrong fact
behind two correct ones), it let pure recommendations get scored as
trivially-true "claims" (diluting the signal), and it never named the
specific overreach pattern above. The rewrite addressed all three:

```python
FACTCHECK_SYSTEM = """...
Split compound sentences into separate claims — if one sentence bundles
three facts, assess each one on its own, so a single wrong fact can't hide
behind two correct ones.

Do NOT extract the debater's overall recommendation, strategy, or proposed
next step ... as a claim to assess — these are not factual assertions...

Mark it unsupported if it:
- states a number, quote, or fact that is not present in the evidence shown
- asserts what a metric or pilot result would prove or "validate" beyond
  what that metric actually measures (e.g. "X% of users trying it would
  validate demand" is overreach unless the evidence itself makes that
  connection — a trial-rate metric proves trial, not demand or retention)
- generalizes from a small evidence sample to a broader population the
  evidence doesn't cover
..."""
```

**Validation, cheaply first:** rather than re-running a full 3-round debate
to test a prompt change, we replayed the exact round that showed the
inconsistency — same arguments, same retrieved evidence — through the new
prompt as a fixture. Result: 17 claims → 0 flagged (old prompt) versus
16 claims → 4 flagged (new prompt), including catching the precise "if X,
we've validated Y" pattern that had slipped through.

**Then a full run to confirm it wasn't a fixture artifact:**

| Run | Claims | Flagged | Rate |
|---|---|---|---|
| `gpt-4o` baseline | 15 | 6 | 40% |
| `reasoning="none"` | 45 | 1 | 2% |
| `reasoning="low"` | 39 | 4 | 10% |
| `medium`, old fact-checker prompt | 40 | 0 | 0% |
| **`medium`, refined fact-checker prompt** | **53** | **12** | **22.6%** |

The rewrite generalized well beyond the one pattern it targeted — it also
caught an advocate claiming a hard spending cap "eliminates" a cost risk the
PRD only ever frames probabilistically, and correctly re-flagged a cost
figure as unsupported in a round where it wasn't actually in the retrieved
evidence, even though it was true elsewhere in the document (the fact-checker
grades claims against *this round's* evidence, by design).

## Stage 6: a live model reliability bug, unrelated to any of the above

The full-run validation trace surfaced something more urgent than a
flag-rate percentage: two of the six arguments in that transcript started
with raw, unexecuted tool-call syntax instead of prose —

```
to=functions.search_internal_context code大小规律
{"query":"feedback.md summaries don't need AI privacy concerns incorrect
summaries manual alternatives"}
The demand evidence is anecdotal, while the PRD explicitly says...
```

The model had attempted a tool call in a format that never resolved into a
structured `function_call` output item — it landed as plain text instead,
and the debater loop's logic (*no structured tool calls → treat the text as
the final argument, verbatim*) shipped it straight into the transcript with
zero validation. This is worse than any fact-check gap: it's garbage text a
real user would see in the UI.

The fix bounds the failure on both cost and reliability:

```python
_LEAKED_TOOL_CALL_RE = re.compile(r'\A\s*to=functions\.\w+.*?\}\s*', re.DOTALL)

# first occurrence -> retry the same request once (likely transient noise)
# second occurrence -> strip it defensively; garbled syntax must never
# reach the transcript regardless of what the model does
```

One retry, only on detection — so the cost impact is nil in the common case.
Verified against both real leaked samples (correctly detected and cleanly
stripped down to just the legitimate argument) and a clean sample (correctly
left untouched, no false positive). Then verified live: a fresh full run hit
the same pattern again — a *different* round and role than either earlier
instance, confirming it's a recurring model behavior, not a fluke — and the
guard caught it, retried, and fell back to a clean strip exactly as designed,
with `[warn] advocate r2: leaked tool-call syntax survived a retry,
stripping before use` visible in server logs for future monitoring.

## Where this left things

- **Model:** `gpt-5.6-luna` via the Responses API (`client.responses.create`
  / `client.responses.parse`), `REASONING_EFFORT = "medium"`.
- **Fact-checker:** rewritten prompt requiring atomic claim decomposition,
  excluding pure recommendations from being scored, and explicitly flagging
  the "metric proves more than it measures" overreach pattern — closing
  most of the gap to the `gpt-4o` baseline's catch rate.
- **Reliability guard:** detect-retry-strip for leaked tool-call syntax, so
  a known model failure mode can never reach the transcript.

## What made this investigation work

- **A single trace tells you what happened; only a diff tells you if it's
  worse.** Every regression here — the repetition, the fact-checker
  collapse, the non-monotonic reasoning-effort result — was invisible
  without a same-question baseline to compare against.
- **Quantify before diagnosing.** "The fact-checker seems lenient" is a
  feeling; "40% → 2%" is a number you can chase down to a root cause and
  verify a fix against.
- **When two near-identical claims get opposite verdicts, that's a spec
  bug, not model noise.** The clearest lead in this whole investigation came
  from lining up two claims with the same shape and different outcomes.
- **Test the cheap thing before the expensive thing.** Replaying one round
  through a revised prompt as a fixture — no debate regeneration required —
  confirmed the fact-checker fix for the cost of a single API call, before
  spending a full 3-round run to confirm it generalized.
- **Not every anomaly is about the thing you're tuning.** The tool-call leak
  had nothing to do with reasoning effort or the fact-checker prompt — it
  just happened to surface while validating them. Chasing the metric you
  came for shouldn't mean ignoring the bug you tripped over on the way.
