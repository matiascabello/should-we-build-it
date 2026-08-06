# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-agent AI debate arena for product go/no-go decisions. Give it a feature question; an **Advocate** and a **Skeptic** argue it over N rounds, each gathering real evidence via tools (internal doc search + live web search) before arguing; a **fact-checker** flags unsupported claims after each round; a **judge** reads the full transcript and emits a structured `DecisionMemo`. Orchestrated as a LangGraph state machine, served by FastAPI, streamed to the browser over SSE.

Full product rationale and design tradeoffs are in `README.md` — read it for *why*, this file is for *how*.

## Commands

```bash
uv sync                                        # install deps (uv-managed, Python >=3.12)
uv run uvicorn server.app:app --reload         # run the app -> http://localhost:8000
```

Required env vars (see `.env`, loaded via `python-dotenv`): `OPENAI_API_KEY`, `SERPER_API_KEY`. Optional: `LANGSMITH_TRACING`, `LANGSMITH_ENDPOINT`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` for tracing (every `client.chat.completions` call and tool call is `@traceable`/wrapped).

There is no test framework (no pytest) — `test_*.py` at the repo root are standalone scripts that call nodes/tools directly and print output for manual inspection, not assertions. Run one directly, e.g.:

```bash
uv run python test_tools.py       # exercise search_internal_context + web_search
uv run python test_debater.py     # run a single advocate_node call
uv run python test_nodes.py       # one full round: advocate -> skeptic -> factcheck -> judge
uv run python test_graph.py       # full compiled graph end-to-end (all rounds + judge)
uv run python test_state.py       # pydantic model construction sanity checks
```
These hit the real OpenAI/Serper APIs (no mocking), so they cost tokens/requests and require the env vars above.

## Architecture

**`graph/state.py`** — the shared state and every Pydantic model that flows through the graph: `ToolCall` (one evidence-gathering action), `Turn` (one debater's argument + its tool calls), `Flag` (fact-checker's verdict on a claim), `DecisionMemo` (judge's final structured output), and `DebateState` (the `TypedDict` LangGraph threads through every node — `transcript` and `flagged_claims` use `Annotated[..., add]` so node returns *append* rather than overwrite).

**`graph/tools.py`** — two tools with a uniform `Evidence` return shape so the fact-checker/UI treat internal and web results identically:
- `search_internal_context` — closed-world RAG over `context/prd.md` + `context/feedback.md`, embedded into an in-memory Chroma store. `_build_retriever()` is a **module-level singleton** — built once on first call and cached for the process lifetime (see "In-flight backend task" below, this is the piece slated to change).
- `web_search` — open-world search via Serper's REST API (`google.serper.dev`), called directly with `requests` (not via the `tavily-python` or `langchain-community` search wrappers, despite `tavily-python` being a listed dependency).
- `TOOL_SCHEMAS` (OpenAI function-calling schemas) and `TOOL_DISPATCH` (name -> callable) are the registry both debater nodes use to offer/execute tools generically.

**`graph/nodes.py`** — all LLM-calling nodes:
- `_run_debater()` is the shared agent loop for both `advocate_node` and `skeptic_node` (they differ only by system prompt/role). It loops on `chat.completions.create` with tools offered, executing whatever the model calls, until the model responds with no more tool calls — that's the final argument. `MAX_TOOL_CALLS = 2` caps evidence-gathering per turn; once hit, tools stop being offered so the model is forced to produce a final answer. `parallel_tool_calls=False` — one search at a time, deliberately, so each tool call's result can be observed before the next.
- `factcheck_node` uses structured output (`client.beta.chat.completions.parse` against a `FlagList` wrapper model) to assess only the *current round's* turns against the evidence those turns actually retrieved — not general truth.
- `judge_node` runs once, after the round loop, consuming the whole transcript + flags and emitting a validated `DecisionMemo` via structured output.
- Model is hardcoded as `MODEL = "gpt-4o"` at module scope — change here if reconfiguring.

**`graph/build.py`** — wires the above into the actual graph. The round loop is a real cycle: `increment_round -> advocate -> skeptic -> factcheck`, then a conditional edge (`_should_continue`) either loops back to `increment_round` or proceeds to `judge -> END`. This cycle is why LangGraph was chosen over a linear chain.

**`server/app.py`** — FastAPI app. `GET /api/debate?question=&max_rounds=` runs `graph.stream(initial, stream_mode="updates")` and re-emits each node's state delta as an SSE event via `_serialize_update`, sleeping 0.4s between events so the frontend can render a "live" unfolding debate rather than a burst. Event `type` is inferred from which state key changed (`transcript` -> `turn`, `flagged_claims` -> `flags`, `verdict` -> `verdict`, bare `round` -> `round`). `GET /` serves `ui/index.html` as a static file (no templating).

## In-flight backend task (see `docs/handoff_ui.md`)

There is a UI build in progress with one deliberate, scoped backend change: making the internal-context retriever **per-request** instead of reading the two fixed files off disk, so users can run the debate on their own question + pasted context. If you're picking that up:
- The only backend surface to touch is the `/api/debate` endpoint (accept context documents in the request) and `_build_retriever()` in `graph/tools.py` (build per-request instead of caching one global retriever off `context/prd.md`/`context/feedback.md`).
- Do **not** change the agents, prompts, fact-checker, judge, graph wiring, or the `Evidence` shape returned to tools — that logic is considered done and tested.
- `docs/handoff_ui.md` also specifies the frontend contract in full (SSE event shapes, layout, the "show the tool calls prominently" requirement, and that `ui/index.html` should stay a single dependency-light file with no build step). Read it before touching either side of this feature — the SSE event format there must stay byte-compatible with what `server/app.py` already emits.
