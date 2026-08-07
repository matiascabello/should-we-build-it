import json
import asyncio
from fastapi import FastAPI
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

    # increment_round emits just the counter — a lightweight "new round" ping
    elif "round" in state_update:
        event["type"] = "round"
        event["round"] = state_update["round"]

    else:
        event["type"] = "other"

    return event


async def debate_stream(question: str, max_rounds: int, context: list[ContextDoc]):
    """Run the graph and yield SSE events, one per node completion.

    Builds a retriever from THIS request's context docs (falling back to
    the default PRD + feedback inside graph/tools.py if none supplied) and
    makes it the active retriever for the duration of the run, so the
    debaters' search_internal_context calls search this run's context —
    isolated from any other concurrent request.
    """
    retriever = build_retriever_from_docs([(d.title, d.content) for d in context])
    token = set_active_retriever(retriever)

    initial = {
        "question": question,
        "max_rounds": max_rounds,
        "round": 0,
        "transcript": [],
        "flagged_claims": [],
        "verdict": None,
    }

    try:
        # "updates" yields {node_name: state_delta} per node finish (turn/flags/
        # verdict/round, as before); "custom" yields each tool call the moment
        # the debater makes it — see graph/nodes.py's stream_writer calls.
        for mode, chunk in graph.stream(initial, stream_mode=["updates", "custom"]):
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
        clear_active_retriever(token)
        if retriever is not None:
            retriever.vectorstore.delete_collection()


@app.post("/api/debate")
async def debate(req: DebateRequest):
    return StreamingResponse(
        debate_stream(req.question, req.max_rounds, req.context),
        media_type="text/event-stream",
    )


@app.get("/")
async def index():
    return FileResponse("ui/index.html")