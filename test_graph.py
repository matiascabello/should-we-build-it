from dotenv import load_dotenv
load_dotenv()

from graph.build import build_debate_graph

graph = build_debate_graph()

initial = {
    "question": "Should we add AI-generated summaries to our note-taking app?",
    "max_rounds": 3,
    "round": 0,
    "transcript": [],
    "flagged_claims": [],
    "verdict": None,
    "rejection": None,
}

final = graph.invoke(initial)

print(f"Rounds run: {final['round']}")
print(f"Turns: {len(final['transcript'])}\n")
for t in final["transcript"]:
    print(f"[R{t.round}] {t.role.upper()}: {t.argument[:90]}...")
    for tc in t.tool_calls:
        print(f"      🔍 {tc.tool}('{tc.query}') -> {tc.source}")

memo = final["verdict"]
print(f"\n=== VERDICT: {memo.recommendation} @ {memo.confidence} ===")
print(memo.confidence_rationale)