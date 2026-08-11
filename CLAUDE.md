# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-agent AI debate arena for product go/no-go decisions. Give it a feature question; an **Advocate** and a **Skeptic** argue it over N rounds, each gathering real evidence via tools (internal doc search + live web search) before arguing; a **fact-checker** flags unsupported claims after each round; a **judge** reads the full transcript and emits a structured `DecisionMemo`. Orchestrated as a LangGraph state machine, served by FastAPI, streamed to the browser over SSE.

Full product rationale and design tradeoffs are in `README.md` — read it for *why*, this file is for *how*. `docs/model-migration-quality-investigation.md` is a detailed case study behind `MODEL`, `REASONING_EFFORT`, and the fact-checker prompt below — read it before changing any of those, it has the evidence for why they're set the way they are.

## Commands

```bash
uv sync                                        # install deps (uv-managed, Python >=3.12)
uv run uvicorn server.app:app --reload         # run the app -> http://localhost:8000
```

Copy `.env.example` to `.env` and fill in real values (`.env` is gitignored — never commit it). Required: `OPENAI_API_KEY`, `SERPER_API_KEY`. Optional: `LANGSMITH_TRACING`, `LANGSMITH_ENDPOINT`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` for tracing (every LLM call and tool call is `@traceable`/wrapped).

There is no test framework (no pytest) — `test_*.py` at the repo root are standalone scripts that call nodes/tools directly and print output for manual inspection, not assertions. Run one directly, e.g.:

```bash
uv run python test_tools.py         # exercise search_internal_context + web_search
uv run python test_scope_check.py   # scope gate on a few in/out-of-scope questions
uv run python test_debater.py       # run a single advocate_node call
uv run python test_nodes.py         # one full round: advocate -> skeptic -> factcheck -> judge
uv run python test_graph.py         # full compiled graph end-to-end (all rounds + judge)
uv run python test_state.py         # pydantic model construction sanity checks
```
These hit the real OpenAI/Serper APIs (no mocking), so they cost tokens/requests and require the env vars above.

## Architecture

**`graph/state.py`** — the shared state and every Pydantic model that flows through the graph: `ToolCall` (one evidence-gathering action, with a `url` populated for web results), `Turn` (one debater's argument + its tool calls), `Flag` (fact-checker's verdict on a claim), `DecisionMemo` (judge's final structured output), `ScopeCheck` (the gate's in/out-of-scope verdict), and `DebateState` (the `TypedDict` LangGraph threads through every node — `transcript` and `flagged_claims` use `Annotated[..., add]` so node returns *append* rather than overwrite; `rejection` is set only when the scope gate refuses a question).

**`graph/tools.py`** — two tools with a uniform `Evidence` return shape (including `url`) so the fact-checker/UI treat internal and web results identically:
- `search_internal_context` — closed-world RAG over the current request's supplied context, falling back to `context/prd.md` + `context/feedback.md` for callers with no request (e.g. the test scripts). `build_retriever_from_docs()` builds a fresh, isolated Chroma collection **per request**; `set_active_retriever()`/`clear_active_retriever()` scope it to the current asyncio task via a contextvar, so concurrent debates never see each other's context. `_get_default_retriever()` is the only cached/singleton retriever, and it exists purely as that no-request fallback.
- `web_search` — open-world search via Serper's REST API (`google.serper.dev`), called directly with `requests` (not via the `tavily-python` or `langchain-community` search wrappers, despite `tavily-python` being a listed dependency).
- `TOOL_SCHEMAS` (Responses-API function-tool schemas — flat shape, no nested `"function"` key the way Chat Completions uses) and `TOOL_DISPATCH` (name -> callable) are the registry both debater nodes use to offer/execute tools generically.

**`graph/nodes.py`** — all LLM-calling nodes, via the **Responses API** (`client.responses.create` / `client.responses.parse`), not Chat Completions:
- `MODEL = "gpt-5.6-luna"`, `REASONING_EFFORT = "medium"` — module-level constants, change here if reconfiguring. Chat Completions rejects function tools + `reasoning_effort` together for this model; Responses supports both, which is why every call goes through it. See the investigation doc for why `medium` specifically — reasoning tier and fact-checker/debate quality turned out not to move together monotonically.
- `scope_check_node` runs first, before any debater/tool cost is spent: a cheap structured-output classification (`text_format=ScopeCheck`) of whether the question is actually a product build/no-build decision, or something else wearing that framing (a bug-fix/homework/general-assistance request). Refusing here — rather than only in the debater prompts — matters because by the time `advocate_node` would run, the round loop is already committed; a system-prompt-only refusal can't stop that loop or skip the tool calls/fact-check/judge cost that follow.
- `_run_debater()` is the shared agent loop for both `advocate_node` and `skeptic_node` (differ only by system prompt/role). Loops on `responses.create` with tools offered, executing whatever the model calls, until a response has no `function_call` output items — that's the final argument. `MAX_TOOL_CALLS = 3` caps evidence-gathering per turn (raised from 2 — at 2, using `web_search` meant giving up an internal-doc call, so it went essentially unused). `parallel_tool_calls=False` — one search at a time, deliberately, so each tool call's result can be observed before the next.
- `_final_answer_text()` and the leak-guard helpers (`_looks_like_leaked_tool_call`, `_strip_leaked_tool_call_syntax`) exist because this model intermittently emits an unexecuted tool-call attempt, or a `commentary`-phase reasoning message, as visible text glued onto the real argument. **Don't remove these as dead code** — they defend against a real, recurring, non-deterministic model failure mode (retry once, then strip defensively so it can never reach the transcript/UI). Full writeup in the investigation doc.
- `factcheck_node` uses structured output (`client.responses.parse`, `text_format=FlagList`) to assess only the *current round's* turns against the evidence those turns actually retrieved — not general truth. The prompt requires splitting compound claims into atomic ones and excludes pure recommendations from being scored at all, after an earlier version was shown to badly under-catch overreach (2% flag rate vs. a 40% baseline — see the investigation doc).
- `judge_node` runs once, after the round loop, consuming the whole transcript + flags and emitting a validated `DecisionMemo` via structured output.

**`graph/build.py`** — wires the above into the actual graph. Every run starts at `scope_check`; a conditional edge (`_route_after_scope_check`) sends a refused question straight to `END` (no debate, no debater/tool/judge cost) or falls into the round loop. The round loop itself is a real cycle: `increment_round -> advocate -> skeptic -> factcheck`, then a conditional edge (`_should_continue`) either loops back to `increment_round` or proceeds to `judge -> END`. This cycle is why LangGraph was chosen over a linear chain.

**`server/app.py`** — FastAPI app. `POST /api/debate` (JSON body: `question`, `max_rounds`, `context` documents) builds a per-request retriever from the supplied context, runs `graph.stream(initial, stream_mode=["updates", "custom"])`, and re-emits each node's state delta as an SSE event via `_serialize_update` — the "updates" stream produces `turn`/`flags`/`verdict`/`round`/`rejected` events; the "custom" stream produces a `tool_call` event the moment each one happens, from `graph/nodes.py`'s `stream_writer` calls. Sleeps 0.4s between "updates" events and 0.25s between live tool calls so the frontend renders a "live" unfolding debate rather than a burst. Rate-limited in-memory (`MAX_CONCURRENT_DEBATES`, `MAX_DEBATES_PER_IP_PER_HOUR`, `MAX_DEBATES_PER_DAY`) since a debate is many real LLM calls, not a cheap request — single-process only, doesn't share state across multiple workers/instances. `GET /` serves `ui/index.html` as a static file (no templating).

**`ui/index.html`** — single dependency-light file, no build step (Tailwind via CDN, IBM Plex via Google Fonts, everything else vanilla JS) — keep it that way. Renders the SSE stream live: turn-cards build up incrementally as tool calls and arguments arrive, a fact-check strip lands per round, and a trailing "thinking" indicator fills the gaps the per-card progress dots don't cover (fact-checking, preparing the next round, judging). Evidence with a `url` (web results) renders as a real link, in both the compact evidence row and the detail modal. A `rejected` event (scope gate refused the question) collapses the optimistically-shown debate scaffold and surfaces the gate's reason in the error banner instead. Has a zero-API-cost "replay" path (`replayExample()`) that plays a saved event sequence through the same renderer as a live debate, for cost-free UI iteration.
