import json
import asyncio
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

from graph.build import build_debate_graph

app = FastAPI()
graph = build_debate_graph()


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
            {"tool": tc.tool, "query": tc.query, "source": tc.source}
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


async def debate_stream(question: str, max_rounds: int):
    """Run the graph and yield SSE events, one per node completion."""
    initial = {
        "question": question,
        "max_rounds": max_rounds,
        "round": 0,
        "transcript": [],
        "flagged_claims": [],
        "verdict": None,
    }

    # stream_mode="updates" yields {node_name: state_delta} per node finish
    for chunk in graph.stream(initial, stream_mode="updates"):
        for node_name, state_update in chunk.items():
            event = _serialize_update(node_name, state_update)
            yield f"data: {json.dumps(event)}\n\n"
            await asyncio.sleep(0.4)  # small pause so the UI feels "live"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


@app.get("/api/debate")
async def debate(question: str, max_rounds: int = 3):
    return StreamingResponse(
        debate_stream(question, max_rounds),
        media_type="text/event-stream",
    )


@app.get("/")
async def index():
    return FileResponse("ui/index.html")