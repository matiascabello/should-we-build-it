import json
import asyncio
import contextlib
import time
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

from graph.build import build_debate_graph
from graph.tools import (
    build_retriever_from_docs, set_active_retriever, clear_active_retriever,
)

app = FastAPI()
graph = build_debate_graph()

# Serves saved debate runs (SSE event sequences) for the UI's zero-API-cost
# "replay" path — see ui/index.html's replayExample().
app.mount("/examples", StaticFiles(directory="examples"), name="examples")

# ---------------------------------------------------------------
# Rate limiting — a real debate isn't a cheap request: 3 rounds means
# multiple sequential LLM calls per debater (graph/nodes.py's REASONING_EFFORT
# and MAX_TOOL_CALLS), plus up to 2 web searches per turn now that debaters
# reach for external evidence by default. An unauthenticated public endpoint
# with no limits is a real cost/abuse exposure, not a hypothetical one.
#
# In-memory and single-process by design: fine for a low-traffic portfolio
# deployment on one instance, but counters reset on restart and don't share
# state across multiple workers/instances — if this ever needs real scale,
# swap these for a shared store (Redis, etc.) rather than trusting this.
#
# All three limits below are starting points, not calibrated to any actual
# budget — tune them to what you're actually willing to spend.
# ---------------------------------------------------------------
MAX_CONCURRENT_DEBATES = 2          # in-flight at once, across all visitors
MAX_DEBATES_PER_IP_PER_HOUR = 3     # per visitor
MAX_DEBATES_PER_DAY = 50            # hard ceiling for the whole deployment

HOUR_SECONDS = 3600
DAY_SECONDS = 86400

_active_debates = 0
_ip_request_times: dict[str, list[float]] = defaultdict(list)
_daily_debate_times: list[float] = []


def _client_ip(request: Request) -> str:
    """Most PaaS deployments (Render, Fly, Railway, etc.) terminate TLS at a
    proxy in front of the app — request.client.host would then be the
    proxy's address for every visitor, collapsing everyone into one
    rate-limit bucket. Prefer X-Forwarded-For's first entry (the original
    client, on platforms that set it correctly) and fall back for local dev."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _reserve_debate_slot(ip: str) -> str | None:
    """Checks all three limits and, only if none are exceeded, atomically
    reserves a slot (increments the counters) — synchronous with no
    `await` inside, so nothing else can interleave between the check and
    the increment on the single event loop. Returns an error message to
    reject with, or None if the request may proceed."""
    global _active_debates
    now = time.time()

    if _active_debates >= MAX_CONCURRENT_DEBATES:
        return "Too many debates running right now — please try again in a minute."

    ip_times = [t for t in _ip_request_times[ip] if now - t < HOUR_SECONDS]
    if len(ip_times) >= MAX_DEBATES_PER_IP_PER_HOUR:
        return f"Rate limit reached ({MAX_DEBATES_PER_IP_PER_HOUR}/hour per visitor) — please try again later."

    global _daily_debate_times
    _daily_debate_times = [t for t in _daily_debate_times if now - t < DAY_SECONDS]
    if len(_daily_debate_times) >= MAX_DEBATES_PER_DAY:
        return "Daily debate limit reached for this deployment — please check back tomorrow."

    _active_debates += 1
    ip_times.append(now)
    _ip_request_times[ip] = ip_times
    _daily_debate_times.append(now)
    return None


def _release_debate_slot() -> None:
    global _active_debates
    _active_debates = max(0, _active_debates - 1)


class ContextDoc(BaseModel):
    title: str = ""
    content: str


class DebateRequest(BaseModel):
    question: str
    max_rounds: int = 3
    context: list[ContextDoc] = Field(default_factory=list)


def _serialize_update(node_name: str, state_update: dict) -> dict:
    """Turn a LangGraph node update into a JSON-friendly event
    the frontend can render. Only forwards what changed."""
    event = {"node": node_name}

    # A debater node emits new transcript turns
    if "transcript" in state_update:
        turn = state_update["transcript"][-1]  # the turn this node added
        event["type"] = "turn"
        event["role"] = turn.role
        event["round"] = turn.round
        event["argument"] = turn.argument
        event["tool_calls"] = [
            {"tool": tc.tool, "query": tc.query, "source": tc.source,
             "snippet": tc.snippet, "url": tc.url}
            for tc in turn.tool_calls
        ]

    # The fact-checker emits new flags
    elif "flagged_claims" in state_update:
        event["type"] = "flags"
        event["flags"] = [
            {"role": f.role, "round": f.round, "claim": f.claim,
             "supported": f.supported, "note": f.note}
            for f in state_update["flagged_claims"]
        ]

    # The judge emits the verdict
    elif "verdict" in state_update and state_update["verdict"]:
        v = state_update["verdict"]
        event["type"] = "verdict"
        event["verdict"] = v.model_dump()

    # The scope gate refused the question — no debate ran at all. (A passing
    # scope check also sets "rejection", to None, which the `and` below skips.)
    elif "rejection" in state_update and state_update["rejection"]:
        event["type"] = "rejected"
        event["reason"] = state_update["rejection"]

    # increment_round emits just the counter — a lightweight "new round" ping
    elif "round" in state_update:
        event["type"] = "round"
        event["round"] = state_update["round"]

    else:
        event["type"] = "other"

    return event


# A single blocking model call (a debater composing its final argument after
# its last tool call, or the fact-checker/judge's one-shot structured-output
# calls) can leave the SSE stream silent for a while — nothing crosses the
# wire until that call returns, since no tool_call or node-update event
# fires mid-call. Verified locally (a proxy that force-closes connections
# idle beyond a few seconds, put in front of the real app) that an
# idle-timeout intermediary can and does kill a "quiet but alive" connection
# during exactly that kind of gap: the debate keeps running and completes
# successfully server-side (LangSmith shows a clean trace, nothing in the
# app's own logs), but the client sees the connection drop as a network
# error. A periodic SSE comment line keeps bytes flowing during any gap long
# enough to be at risk — the frontend's parser already ignores any line that
# doesn't start with "data:", so this needs no client-side change.
HEARTBEAT_INTERVAL_SECONDS = 15


async def _drain_graph(initial: dict, queue: asyncio.Queue):
    """Runs the graph to completion, pushing every (mode, chunk) update into
    the queue as it arrives. Runs as its own task, separate from whatever is
    reading the queue, so a heartbeat timeout on the read side (see
    debate_stream) never cancels — or otherwise disturbs — the graph's own
    execution; it only ever stops waiting on an empty queue."""
    try:
        # astream(), not stream(): graph/nodes.py's node functions are plain
        # sync def's making blocking OpenAI/Serper calls. Under the sync
        # .stream() API those ran in-line on this event loop's own thread,
        # starving every other request on the process — including Render's
        # health check, which is what killed the instance mid-debate the
        # first time this ran in production. Under .astream(), LangGraph
        # dispatches each sync node to a thread pool automatically (see
        # langgraph/_internal/_runnable.py's coerce_to_runnable: a plain
        # callable's async path is `run_in_executor`), so the event loop
        # stays free for the whole run.
        #
        # "updates" yields {node_name: state_delta} per node finish (turn/flags/
        # verdict/round, as before); "custom" yields each tool call the moment
        # the debater makes it — see graph/nodes.py's stream_writer calls.
        async for mode, chunk in graph.astream(initial, stream_mode=["updates", "custom"]):
            await queue.put(("item", mode, chunk))
    except Exception as exc:
        # Re-raised on the consumer side (debate_stream) so a real graph
        # failure still propagates the same way it did before this queue
        # existed, instead of vanishing into an unread task exception.
        await queue.put(("error", exc, None))
    else:
        await queue.put(("done", None, None))


async def debate_stream(question: str, max_rounds: int, context: list[ContextDoc]):
    """Run the graph and yield SSE events, one per node completion.

    Builds a retriever from THIS request's context docs (falling back to
    the default PRD + feedback inside graph/tools.py if none supplied) and
    makes it the active retriever for the duration of the run, so the
    debaters' search_internal_context calls search this run's context —
    isolated from any other concurrent request.
    """
    # build_retriever_from_docs does synchronous embedding calls — off the
    # event loop thread for the same reason graph.astream() is used below.
    retriever = await asyncio.to_thread(
        build_retriever_from_docs, [(d.title, d.content) for d in context]
    )
    token = set_active_retriever(retriever)

    initial = {
        "question": question,
        "max_rounds": max_rounds,
        "round": 0,
        "transcript": [],
        "flagged_claims": [],
        "verdict": None,
        "rejection": None,
    }

    # The graph runs in its own task so the heartbeat loop below can wait on
    # the queue with a timeout without ever touching (or cancelling) it.
    queue: asyncio.Queue = asyncio.Queue()
    producer = asyncio.create_task(_drain_graph(initial, queue))

    try:
        while True:
            try:
                kind, a, b = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue

            if kind == "done":
                break
            if kind == "error":
                raise a

            mode, chunk = a, b
            if mode == "custom":
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.25)  # tool calls arrive in a burst; keep it snappy
                continue
            for node_name, state_update in chunk.items():
                event = _serialize_update(node_name, state_update)
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0.4)  # small pause so the UI feels "live"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    finally:
        # Guards against an early client disconnect (StreamingResponse
        # closes this generator via GeneratorExit, which still runs this
        # finally block) leaving the graph running — and burning API calls —
        # for a debate nobody's listening to anymore. A no-op if the
        # producer already finished on its own.
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer
        clear_active_retriever(token)
        if retriever is not None:
            retriever.vectorstore.delete_collection()
        # The slot was reserved by the route handler before this generator
        # started (see _reserve_debate_slot) — release it here, in this
        # generator's own finally, since that's the debate's real lifetime:
        # the whole SSE stream, including an early client disconnect
        # (StreamingResponse closes the generator via GeneratorExit, which
        # still runs this finally block).
        _release_debate_slot()


@app.post("/api/debate")
async def debate(req: DebateRequest, request: Request):
    rejection = _reserve_debate_slot(_client_ip(request))
    if rejection:
        raise HTTPException(status_code=429, detail=rejection)
    return StreamingResponse(
        debate_stream(req.question, req.max_rounds, req.context),
        media_type="text/event-stream",
    )


@app.get("/")
async def index():
    return FileResponse("ui/index.html")