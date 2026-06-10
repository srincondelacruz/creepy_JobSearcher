"""Conditional routing logic for the agent graph.

Routers are pure functions: read state, return the name of the next node (a string
that maps to an edge in agent_graph.py). Keeping them isolated makes the control flow
testable without invoking the LLM.
"""
from __future__ import annotations

from modules.graph.state import AgentState


def route_after_fetch(state: AgentState) -> str:
    """Stop early on fetch failure, otherwise proceed to analysis."""
    if state.get("next_action") == "error" or not state.get("offer_content"):
        return "error"
    return "analyze_offer"


def route_after_questionnaire(state: AgentState) -> str:
    """Branch on whether a questionnaire was found."""
    if state.get("has_questionnaire") and state.get("questionnaire"):
        return "generate_responses"
    return "generate_recommendation"
