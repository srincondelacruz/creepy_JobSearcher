"""LangGraph state definition for the job-analysis agent."""
from __future__ import annotations

from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """Shared state passed between graph nodes.

    `total=False` lets nodes return partial updates — LangGraph merges them.
    `messages` uses the add_messages reducer so chat history accumulates across
    nodes and across runs (with the checkpointer) instead of being overwritten.
    """

    # ── Inputs ────────────────────────────────────────────────────────────────
    url: str                          # offer URL to analyze
    raw_questions: list[str]          # questions provided directly (skip extraction)
    dry_run: bool                     # suppress notifications + external side effects

    # ── Fetched / derived data ────────────────────────────────────────────────
    offer_content: str                # full extracted offer text
    offer_meta: dict[str, Any]        # {title, company, location, salary, source_url}
    offer_analysis: dict[str, Any]    # {score, priority, strengths, gaps, objections, ...}
    questionnaire: list[str]          # extracted questionnaire questions
    has_questionnaire: bool           # routing flag
    responses: list[dict[str, Any]]   # [{question, answer, language}]
    recommendation: dict[str, Any]    # {apply, reasoning, cv_tips, cover_letter}

    # ── Context ───────────────────────────────────────────────────────────────
    context: str                      # merged profile + Obsidian context
    profile: dict[str, Any]           # parsed profile.yaml

    # ── Control / bookkeeping ─────────────────────────────────────────────────
    next_action: str                  # router decision (set by nodes, read by edges)
    errors: list[str]                 # accumulated non-fatal node errors
    messages: Annotated[list, add_messages]   # chat-style log (LLM turns, events)


def new_state(url: str = "", dry_run: bool = False, **kwargs) -> AgentState:
    """Build a fresh state with sane defaults."""
    base: AgentState = {
        "url": url,
        "raw_questions": [],
        "dry_run": dry_run,
        "offer_content": "",
        "offer_meta": {},
        "offer_analysis": {},
        "questionnaire": [],
        "has_questionnaire": False,
        "responses": [],
        "recommendation": {},
        "context": "",
        "profile": {},
        "next_action": "",
        "errors": [],
        "messages": [],
    }
    base.update(kwargs)  # type: ignore[typeddict-item]
    return base
