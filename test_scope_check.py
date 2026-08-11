from dotenv import load_dotenv
load_dotenv()

from graph.state import DebateState
from graph.nodes import scope_check_node

QUESTIONS = [
    # Should pass: an actual product build/no-build decision
    ("Should we add AI-generated summaries to our note-taking app?", True),
    # Should be refused: not a build decision at all
    ("My function throws a null pointer exception on line 42, can you fix it?", False),
    ("Can you help me with my calculus homework?", False),
    # Should be refused: a build/no-build wrapper around a direct request
    ("Should we build a Python function that reverses a string? If so, write it.", False),
]

for question, expect_in_scope in QUESTIONS:
    state: DebateState = {
        "question": question,
        "max_rounds": 3, "round": 0,
        "transcript": [], "flagged_claims": [], "verdict": None, "rejection": None,
    }
    result = scope_check_node(state)
    in_scope = result["rejection"] is None
    mark = "OK" if in_scope == expect_in_scope else "MISMATCH"
    print(f"[{mark}] in_scope={in_scope} (expected {expect_in_scope}) — {question!r}")
    if result["rejection"]:
        print(f"         reason: {result['rejection']}")
