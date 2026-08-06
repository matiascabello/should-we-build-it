# UI Handoff — "Should We Build It?" Debate Arena

This document briefs you (Claude Code) on building the frontend for an existing,
working backend — plus **one deliberate backend change** described below.

The debate engine (the LangGraph state machine, the agents, the fact-checker,
the judge) is done and tested. **Do not change the debate logic, the prompts,
the agent behavior, or the graph wiring.** The only backend work in scope is the
context-input change in the "Backend changes" section — and if you find you need
any other backend change to make the UI work, flag it and ask rather than
changing it silently.

## What the app is

A multi-agent debate arena that answers "should we build this feature?". Two
LLM agents — an **Advocate** and a **Skeptic** — debate a product decision
across 3 rounds. Each agent gathers real evidence mid-argument using tools
(internal document search + live web search). A **fact-checker** flags
unsupported claims after each round. A **judge** reads the whole debate and
emits a structured decision memo. It runs on a LangGraph state machine and
streams to the browser node-by-node.

**The single most important thing this UI must convey:** that these are *agents
doing research*, not two chatbots exchanging opinions. The evidence they gather
and the fact-checks on their claims are the proof of that — treat them as
first-class UI, not decoration. (See "Priority: show the tool calls" below.)

---

## Backend changes (in scope — this is the one exception)

Today the agents' internal knowledge is hardcoded: the retriever reads two fixed
files (`context/prd.md`, `context/feedback.md`) off disk. We want users to be
able to run the debate on **their own** decision, so the context must become
per-request instead of hardcoded.

**What to change:**
- The `/api/debate` endpoint must accept **context documents** supplied with the
  request (in addition to the existing `question` and `max_rounds`).
- The retriever in `graph/tools.py` must build its index from the
  **request-supplied context** for that run, rather than always reading the two
  files on disk. (The current `_build_retriever()` caches a single global
  retriever off the fixed files — that needs to become per-request context.)
- Keep it simple: context can arrive as one or more text blobs (a title + body
  each is fine). You do **not** need file parsing beyond plain text / Markdown.

**What NOT to change:** the agents, prompts, fact-checker, judge, graph
structure, or the *shape* of what the retriever returns to the tools (the
`Evidence` objects). You're changing *where the documents come from*, nothing
about how they're searched or used.

Flag clearly in your PR/notes that you modified the retriever and endpoint, and
keep the change minimal and readable — this is a portfolio repo.

---

## The case system (what the user picks or supplies)

A **case** = a product question + its context documents. The UI is built around
choosing or creating a case, then running the debate on it.

- **Predefined cases:** ship a small set of ready-to-run cases the user can pick
  from a selector. The **note-taking-app summaries** case is the default and
  must be preloaded on first paint (its question + the existing PRD and feedback
  content). Structure this so adding more predefined cases later is trivial —
  e.g. a small array/JSON of cases the picker renders from. Two or three cases
  total is plenty for this build; the note-app one is the essential one.
- **Bring your own:** the user can also create a case by entering their own
  question and pasting their own context text. A basic path is enough:
  - a **question** field, and
  - a **context** area where they can paste text (a PRD, feedback, notes).
    Allowing a couple of separately-labeled boxes (e.g. "Spec" and "Feedback")
    or one combined box is your call — whichever reads cleaner. Attaching an
    `.md` file is a nice-to-have, not required; pasting is the must-have.
- Whichever case is active (predefined or user-made), "Start debate" sends its
  question + context to the (now context-aware) `/api/debate` endpoint.

---

## Priority: show the tool calls

This is a **primary requirement**, not a card detail. The tool calls are the
visible proof that the agents are researching rather than free-associating, so
they must be prominent and legible.

For every turn, render each tool call the agent made as a distinct, readable
element — not hidden behind a hover or collapsed by default. Show:
- **which tool** was used, visually distinguished:
  `search_internal_context` → an "internal doc" treatment (e.g. 📄, one color),
  `web_search` → a "web" treatment (e.g. 🌐, another color);
- **the query** the agent chose (this is the agent's *reasoning* made visible —
  it's the good part);
- **the source** it got back (a filename for internal, a page title for web).

A viewer skimming a single screenshot should be able to see "the Advocate
searched the web for market size and the internal feedback for demand, then
argued." Make that trivially readable. If anything gets prominence in a turn
card, it's the evidence trail.

---

## The backend contract (what you build against)

- **Server:** FastAPI, run with `uv run uvicorn server.app:app --reload`
- **Page route:** `GET /` returns `ui/index.html` (the file you're building).
- **Stream route:** `GET/POST /api/debate` returns an **SSE stream**
  (`text/event-stream`). Consume it in the browser. Note: because context can
  now be sizeable, you may switch this to POST with a JSON body (question +
  context + max_rounds) and consume the stream via `fetch()` + a stream reader,
  rather than `EventSource` (which is GET-only). Either approach is fine —
  choose what's cleanest given the context payload. Keep the **event format
  below unchanged.**

### SSE events (unchanged — the frontend switches on `event.type`)

**`round`** — a new round is starting.
```json
{ "node": "increment_round", "type": "round", "round": 1 }
```

**`turn`** — a debater finished. The main event.
```json
{
  "node": "advocate", "type": "turn",
  "role": "advocate",          // "advocate" | "skeptic"
  "round": 1,
  "argument": "Full argument text...",
  "tool_calls": [
    { "tool": "search_internal_context", "query": "user demand", "source": "feedback.md" },
    { "tool": "web_search", "query": "market size", "source": "AI Note Taking Market..." }
  ]
}
```

**`flags`** — the fact-checker assessed the round's claims.
```json
{
  "node": "factcheck", "type": "flags",
  "flags": [
    { "role": "skeptic", "round": 1,
      "claim": "Monthly cost is $2,500-$6,000",
      "supported": false,
      "note": "Not backed by retrieved evidence." }
  ]
}
```

**`verdict`** — the judge's final memo (arrives once, near the end).
```json
{
  "node": "judge", "type": "verdict",
  "verdict": {
    "recommendation": "build_descoped",   // "build" | "dont_build" | "build_descoped"
    "confidence": 0.6,                     // 0.0–1.0
    "confidence_rationale": "…",
    "key_tradeoffs": ["…", "…"],
    "what_would_change_my_mind": ["…", "…"],
    "open_questions": ["…", "…"]
  }
}
```

**`done`** — stream is over. Close the connection.
```json
{ "type": "done" }
```

---

## Layout

- **Top / setup:** a case picker (predefined cases + "bring your own"), the
  active case's question, an editable context area, and a rounds selector
  (default 3). A "Start debate" button kicks off the run.
- **Debate view — two columns:** Advocate on the left, Skeptic on the right.
  Each `turn` event drops a card into the matching column, in round order.
- **Turn card:** round number, the argument, and — prominently — the evidence
  trail (see "Priority: show the tool calls").
- **Fact-check flags:** when a `flags` event arrives, attach flags to the
  relevant round. **Unsupported** claims must be clearly visible (⚠️ with the
  claim + note). Supported claims can be subtle (a small ✓) or summarized to
  avoid noise — your call, but the unsupported ones can't be missable.
- **Verdict panel:** on `verdict`, reveal a prominent memo below the columns.
  Map the recommendation enum to friendly text ("Build", "Don't build",
  "Build, but descope"), show confidence as a meter (0–1 → bar or %), then
  rationale, tradeoffs, what-would-change-my-mind, and open questions as
  readable sections.

---

## The quality bar (read this carefully)

Two separate things that got conflated before — keep them separate:

1. **Keep the setup dependency-light.** No build step, no bundler, no framework
   toolchain to install. A single `ui/index.html` served by the existing
   FastAPI static route is the target. CDN-loaded libraries are fine if they
   earn their place. This is about *ease of running the repo*, nothing else.

2. **Make it genuinely polished.** "No build step" is **not** permission to look
   plain or default. This is a portfolio piece and the visual craft is part of
   what's being judged. That means: considered typography (not system-default
   everything), a deliberate and restrained color palette, real spacing and
   rhythm, and tasteful motion (cards entering as the debate unfolds; a subtle
   "thinking…" state in the active column between turns). It should look like
   someone with taste designed it on purpose.

   **Read the `frontend-design` skill before starting** and follow it — it
   covers the design direction and styling constraints for exactly this. Aim for
   something distinctive and intentional, not a bootstrap-looking template.

The backend already paces events ~0.4s apart, so the "unfolding" rhythm is there
for you to lean into.

The design goal, restated: someone landing on a **screenshot** should instantly
read "two agents argued, they searched for evidence, claims got checked, a
judgment came out" — *and* think "this looks well made."

---

## Nice-to-haves (only if time allows)
- Attach an `.md` file for context (pasting is the required path).
- Replay a saved debate from a JSON file in `examples/` so the demo runs with no
  API keys — great for a README GIF.
- A cost/token readout.
- Mobile: collapse the two columns into a single stream.

## Explicitly out of scope
- Auth, persistence, multi-user, deployment config.
- Any change to the debate logic, prompts, agents, fact-checker, judge, or graph
  (the context-input change to the retriever/endpoint is the ONLY backend work).
- Per-token streaming — the backend streams per-node by design.

## Test it
1. `uv run uvicorn server.app:app --reload`
2. Open `http://localhost:8000/`
3. Confirm the note-app case is preloaded; start it and watch turns stream in,
   tool calls render prominently, flags attach, and the verdict appears.
4. Create a "bring your own" case: paste a question + some context, run it, and
   confirm the agents search *that* context (not the old hardcoded files).
5. Sanity-check the raw stream if needed with a direct request to `/api/debate`.