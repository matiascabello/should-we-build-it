from dotenv import load_dotenv
load_dotenv()

from graph.state import DebateState
from graph.nodes import advocate_node, skeptic_node, factcheck_node, judge_node

state: DebateState = {
    "question": "Should we add AI-generated summaries to our note-taking app?",
    "max_rounds": 1, "round": 1,
    "transcript": [], "flagged_claims": [], "verdict": None,
}

# One round: advocate, then skeptic
state["transcript"] += advocate_node(state)["transcript"]
state["transcript"] += skeptic_node(state)["transcript"]
for t in state["transcript"]:
    print(f"[{t.role}] {t.argument}\n")

# Fact-check the round
state["flagged_claims"] += factcheck_node(state)["flagged_claims"]
print("FLAGS:")
for f in state["flagged_claims"]:
    mark = "OK" if f.supported else "UNSUPPORTED"
    print(f"  [{mark}] ({f.role}) {f.claim}")

# Judge
memo = judge_node(state)["verdict"]
print(f"\nVERDICT: {memo.recommendation} @ {memo.confidence}")
print("Rationale:", memo.confidence_rationale)
print("Would change my mind:", memo.what_would_change_my_mind)
print("Open questions:", memo.open_questions)