from langgraph.graph import StateGraph, START, END
from graph.state import DebateState
from graph.nodes import (
    scope_check_node, advocate_node, skeptic_node, factcheck_node, judge_node,
)


def _increment_round(state: DebateState) -> dict:
    """Tiny node: bump the round counter at the start of each round."""
    return {"round": state["round"] + 1}


def _should_continue(state: DebateState) -> str:
    """Conditional edge: loop for another round, or go to the judge."""
    if state["round"] < state["max_rounds"]:
        return "next_round"
    return "judge"


def _route_after_scope_check(state: DebateState) -> str:
    """Conditional edge: an off-topic question skips the debate entirely —
    no advocate/skeptic/factcheck/judge cost is spent on it."""
    return "reject" if state.get("rejection") else "proceed"


def build_debate_graph():
    g = StateGraph(DebateState)

    g.add_node("scope_check", scope_check_node)
    g.add_node("increment_round", _increment_round)
    g.add_node("advocate", advocate_node)
    g.add_node("skeptic", skeptic_node)
    g.add_node("factcheck", factcheck_node)
    g.add_node("judge", judge_node)

    # Every run is gated first: reject off-topic questions straight to END,
    # otherwise fall into the normal round loop.
    g.add_edge(START, "scope_check")
    g.add_conditional_edges(
        "scope_check",
        _route_after_scope_check,
        {"proceed": "increment_round", "reject": END},
    )

    # A round runs: bump counter -> advocate -> skeptic -> fact-check
    g.add_edge("increment_round", "advocate")
    g.add_edge("advocate", "skeptic")
    g.add_edge("skeptic", "factcheck")

    # After fact-check: either loop back for another round, or judge.
    g.add_conditional_edges(
        "factcheck",
        _should_continue,
        {"next_round": "increment_round", "judge": "judge"},
    )

    g.add_edge("judge", END)
    return g.compile()