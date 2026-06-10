"""LangGraph assembly for the on-demand job-analysis agent.

Flow:

    START
      │
      ▼
    load_context ──► fetch_offer ──(router)──► [error] ─► handle_error ─► END
                                       │
                                       ▼
                                 analyze_offer
                                       │
                                       ▼
                              extract_questionnaire
                                       │
                                   (router)
                              ┌────────┴─────────┐
                  has questions             no questions
                              │                  │
                              ▼                  │
                     generate_responses          │
                              │                  │
                              └────────┬─────────┘
                                       ▼
                            generate_recommendation
                                       │
                                       ▼
                                    notify ─► END

Extensible: add a node with `builder.add_node(...)` and an edge. Dependencies are
injected via GraphNodes, so the graph definition stays declarative.
"""
from __future__ import annotations

from pathlib import Path

from langgraph.graph import StateGraph, START, END
from loguru import logger

from modules.graph.nodes import GraphNodes
from modules.graph.router import route_after_fetch, route_after_questionnaire
from modules.graph.state import AgentState
from modules.memory.persistence import build_checkpointer


def build_graph(nodes: GraphNodes, checkpointer=None):
    """Compile and return the agent graph."""
    builder = StateGraph(AgentState)

    # Register nodes
    builder.add_node("load_context", nodes.load_context)
    builder.add_node("fetch_offer", nodes.fetch_offer)
    builder.add_node("analyze_offer", nodes.analyze_offer)
    builder.add_node("extract_questionnaire", nodes.extract_questionnaire)
    builder.add_node("generate_responses", nodes.generate_responses)
    builder.add_node("generate_recommendation", nodes.generate_recommendation)
    builder.add_node("notify", nodes.notify)
    builder.add_node("handle_error", nodes.handle_error)

    # Linear entry
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "fetch_offer")

    # Branch on fetch result
    builder.add_conditional_edges(
        "fetch_offer",
        route_after_fetch,
        {"analyze_offer": "analyze_offer", "error": "handle_error"},
    )

    builder.add_edge("analyze_offer", "extract_questionnaire")

    # Branch on questionnaire presence
    builder.add_conditional_edges(
        "extract_questionnaire",
        route_after_questionnaire,
        {
            "generate_responses": "generate_responses",
            "generate_recommendation": "generate_recommendation",
        },
    )

    builder.add_edge("generate_responses", "generate_recommendation")
    builder.add_edge("generate_recommendation", "notify")
    builder.add_edge("notify", END)
    builder.add_edge("handle_error", END)

    graph = builder.compile(checkpointer=checkpointer)
    logger.debug("Agent graph compiled")
    return graph


class JobAgentGraph:
    """High-level wrapper: builds nodes + checkpointer + compiled graph, runs analyses."""

    def __init__(self, settings, profile, obsidian_memory, fetcher, db,
                 base_dir: Path, notifier=None):
        self.settings = settings
        self.recursion_limit = settings.get("graph", {}).get("recursion_limit", 25)
        self.nodes = GraphNodes(settings, profile, obsidian_memory, fetcher, db, notifier)
        self.checkpointer = build_checkpointer(settings, base_dir)
        self.graph = build_graph(self.nodes, self.checkpointer)

    def analyze_url(self, url: str, thread_id: str | None = None,
                    dry_run: bool = False, raw_questions=None) -> AgentState:
        """Run the full graph for a single URL. thread_id enables persistent memory."""
        from modules.graph.state import new_state
        from modules.memory.persistence import thread_config

        thread = thread_id or _thread_from_url(url)
        config = thread_config(thread, self.recursion_limit)
        initial = new_state(url=url, dry_run=dry_run, raw_questions=raw_questions or [])

        logger.info(f"Running graph for {url} (thread={thread})")
        final_state = self.graph.invoke(initial, config=config)
        return final_state


def _thread_from_url(url: str) -> str:
    import hashlib
    return "url-" + hashlib.sha256(url.encode()).hexdigest()[:12]
