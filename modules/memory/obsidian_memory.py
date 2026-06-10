"""Obsidian vault integration.

Recursively scans an Obsidian vault, extracts relevant markdown notes, and builds
an enriched context string injected into the LLM system prompt alongside profile.yaml.

Notes are prioritised over the static YAML profile because they hold the most recent
information: active application processes, researched companies, interview notes, etc.

A 1-hour TTL cache avoids re-reading the whole vault on every call.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger


@dataclass
class _Note:
    path: Path
    title: str
    content: str
    mtime: float


def _strip_frontmatter(md_text: str) -> str:
    """Remove YAML frontmatter (--- ... ---) but keep any tags line as a hint."""
    stripped = md_text.strip()
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            return stripped[end + 3:].strip()
    return stripped


class ObsidianMemory:
    """Vault scanner with TTL cache."""

    def __init__(self, settings: dict):
        obs_cfg = settings.get("obsidian", {})
        self.vault_path: Optional[Path] = (
            Path(settings["obsidian_vault_path"])
            if settings.get("obsidian_vault_path")
            else None
        )
        self.ttl = obs_cfg.get("cache_ttl_seconds", 3600)
        self.max_notes = obs_cfg.get("max_notes", 100)
        self.max_chars_per_note = obs_cfg.get("max_chars_per_note", 4000)
        self.max_total_chars = obs_cfg.get("max_total_chars", 40000)
        self.exclude_dirs = set(
            obs_cfg.get(
                "exclude_dirs",
                [".obsidian", ".trash", "templates", "Templates", "attachments"],
            )
        )

        self._cache: str = ""
        self._cache_ts: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def get_context(self, force_refresh: bool = False) -> str:
        """Return enriched context string. Cached for `ttl` seconds."""
        if not self.vault_path:
            return ""
        if not self.vault_path.exists():
            logger.warning(f"Obsidian vault not found: {self.vault_path}")
            return ""

        now = time.time()
        if not force_refresh and self._cache and (now - self._cache_ts) < self.ttl:
            logger.debug("Obsidian context served from cache")
            return self._cache

        logger.info(f"Scanning Obsidian vault: {self.vault_path}")
        try:
            notes = self._scan_vault()
            self._cache = self._build_context(notes)
            self._cache_ts = now
            logger.info(f"Obsidian scan complete: {len(notes)} notes, {len(self._cache)} chars")
        except Exception as e:
            logger.error(f"Obsidian scan failed: {e}")
            # Serve stale cache if we have one, else empty
            return self._cache or ""
        return self._cache

    def invalidate(self) -> None:
        self._cache_ts = 0.0

    # ── Internal ──────────────────────────────────────────────────────────────

    def _is_excluded(self, path: Path) -> bool:
        return any(part in self.exclude_dirs for part in path.parts)

    def _scan_vault(self) -> list[_Note]:
        notes: list[_Note] = []
        for md_path in self.vault_path.rglob("*.md"):
            if self._is_excluded(md_path.relative_to(self.vault_path)):
                continue
            try:
                raw = md_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as e:
                logger.debug(f"Skipping unreadable note {md_path}: {e}")
                continue
            body = _strip_frontmatter(raw)
            if not body.strip():
                continue
            notes.append(
                _Note(
                    path=md_path,
                    title=md_path.stem,
                    content=body[: self.max_chars_per_note],
                    mtime=md_path.stat().st_mtime,
                )
            )

        # Most recently edited notes first — recency = relevance for job search
        notes.sort(key=lambda n: n.mtime, reverse=True)
        return notes[: self.max_notes]

    def _build_context(self, notes: list[_Note]) -> str:
        if not notes:
            return ""
        chunks: list[str] = ["## OBSIDIAN VAULT CONTEXT (most recent first)\n"]
        total = 0
        for note in notes:
            rel = note.path.name
            block = f"### Note: {note.title} ({rel})\n{note.content.strip()}\n"
            if total + len(block) > self.max_total_chars:
                chunks.append(f"\n_[{len(notes)} notes total — remainder truncated for length]_")
                break
            chunks.append(block)
            total += len(block)
        return "\n".join(chunks)


def load_obsidian_memory(settings: dict) -> ObsidianMemory:
    return ObsidianMemory(settings)
