"""Checkpointer factory for LangGraph persistent memory.

Prefers SqliteSaver so analyzed offers, active processes, and conversation context
survive across sessions. Falls back to in-memory MemorySaver if the sqlite backend
isn't installed (pip install langgraph-checkpoint-sqlite).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from loguru import logger


def build_checkpointer(settings: dict, base_dir: Path):
    """Return a LangGraph checkpointer (SqliteSaver if possible, else MemorySaver)."""
    graph_cfg = settings.get("graph", {})
    db_rel = graph_cfg.get("checkpoint_db", "data/graph_memory.sqlite")
    db_path = base_dir / db_rel
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        # check_same_thread=False: bot + scheduler may touch it from different threads
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        saver = SqliteSaver(conn)
        logger.info(f"Graph persistence: SqliteSaver at {db_path}")
        return saver
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver

        logger.warning(
            "langgraph-checkpoint-sqlite not installed — using in-memory MemorySaver "
            "(state will NOT persist across restarts). "
            "Install with: pip install langgraph-checkpoint-sqlite"
        )
        return MemorySaver()


def thread_config(thread_id: str, recursion_limit: int = 25) -> dict:
    """Build the `config` dict passed to graph.invoke()."""
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
