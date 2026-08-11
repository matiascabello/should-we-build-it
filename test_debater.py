from dotenv import load_dotenv
load_dotenv()

from graph.state import DebateState
from graph.nodes import advocate_node

state: DebateState = {
    "question": "Should we add AI-generated summaries to our note-taking app?",
    "max_rounds": 3,
    "round": 1,
    "transcript": [],
    "flagged_claims": [],
    "verdict": None,
    "rejection": None,
}

result = advocate_node(state)
turn = result["transcript"][0]

print("ARGUMENT:\n", turn.argument, "\n")
print("TOOL CALLS:")
for tc in turn.tool_calls:
    print(f"  [{tc.tool}] q='{tc.query}' -> {tc.source}: {tc.snippet[:60]}...")