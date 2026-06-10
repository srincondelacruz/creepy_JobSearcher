"""Graph nodes. Each method takes AgentState and returns a partial state update.

Dependencies (LLM, fetcher, memory, db, profile) are injected via the constructor so
nodes stay pure-ish and the graph wiring stays declarative. Add new nodes by adding a
method here and a node/edge in agent_graph.py.
"""
from __future__ import annotations

import yaml
from loguru import logger

from modules.generator.prompts import (
    GRAPH_SYSTEM_PROMPT,
    ANALYZE_OFFER_PROMPT,
    EXTRACT_QUESTIONNAIRE_PROMPT,
    RECOMMENDATION_PROMPT,
    QUESTIONNAIRE_USER_PROMPT,
    COVER_LETTER_USER_PROMPT,
)
from modules.graph.llm import LLM
from modules.graph.state import AgentState


class GraphNodes:
    def __init__(
        self,
        settings: dict,
        profile: dict,
        obsidian_memory,
        fetcher,
        db,
        notifier=None,
    ):
        self.settings = settings
        self.profile = profile
        self.obsidian = obsidian_memory
        self.fetcher = fetcher
        self.db = db
        self.notifier = notifier

        self.llm = LLM(settings)                 # opus-4-8 for reasoning
        self.llm_fast = LLM(settings, use_fast=True)  # haiku for light extraction
        self._profile_yaml = yaml.dump(profile, allow_unicode=True, default_flow_style=False)

    # ── load_context ──────────────────────────────────────────────────────────

    def load_context(self, state: AgentState) -> dict:
        """Build the full candidate context: profile.yaml + live Obsidian vault."""
        logger.info("[node] load_context")
        obsidian_ctx = ""
        try:
            obsidian_ctx = self.obsidian.get_context()
        except Exception as e:
            logger.warning(f"Obsidian context unavailable: {e}")

        # Obsidian notes come FIRST — most recent info takes priority over static YAML
        parts = []
        if obsidian_ctx:
            parts.append(obsidian_ctx)
        parts.append("## STRUCTURED PROFILE (profile.yaml)\n" + self._profile_yaml)
        context = "\n\n".join(parts)

        return {
            "context": context,
            "profile": self.profile,
            "messages": [("system", "context loaded")],
        }

    # ── fetch_offer ───────────────────────────────────────────────────────────

    def fetch_offer(self, state: AgentState) -> dict:
        """Extract offer content from the URL. Robust: never raises, sets errors."""
        url = state.get("url", "").strip()
        logger.info(f"[node] fetch_offer: {url}")
        if not url:
            return {"next_action": "error", "errors": ["No URL provided"]}

        try:
            result = self.fetcher.fetch(url)
        except Exception as e:
            logger.error(f"fetch_offer crashed: {e}")
            return {"next_action": "error", "errors": [f"fetch crashed: {e}"]}

        if not result.ok:
            logger.error(f"fetch_offer failed: {result.error}")
            return {"next_action": "error", "errors": [f"fetch failed: {result.error}"]}

        logger.info(f"Fetched {len(result.text)} chars via {result.method}")
        return {
            "offer_content": result.text,
            "offer_meta": result.to_meta(),
            "next_action": "analyze",
            "messages": [("system", f"offer fetched via {result.method}")],
        }

    # ── analyze_offer ─────────────────────────────────────────────────────────

    def analyze_offer(self, state: AgentState) -> dict:
        logger.info("[node] analyze_offer")
        meta = state.get("offer_meta", {})
        prompt = ANALYZE_OFFER_PROMPT.format(
            source_url=meta.get("source_url", state.get("url", "")),
            offer_content=state.get("offer_content", "")[:8000],
        )
        analysis = self.llm.complete_json(
            self._system(state),
            prompt,
            default={"score": 5, "priority": "medium", "reasoning": "analysis unavailable"},
        )
        analysis["score"] = max(1, min(10, int(analysis.get("score", 5))))
        logger.info(f"Fit score: {analysis['score']}/10 ({analysis.get('priority')})")
        return {
            "offer_analysis": analysis,
            "messages": [("assistant", f"analyzed: {analysis['score']}/10")],
        }

    # ── extract_questionnaire ─────────────────────────────────────────────────

    def extract_questionnaire(self, state: AgentState) -> dict:
        logger.info("[node] extract_questionnaire")
        # If caller passed questions explicitly, trust them and skip extraction
        raw_qs = state.get("raw_questions") or []
        if raw_qs:
            return {"questionnaire": raw_qs, "has_questionnaire": True}

        prompt = EXTRACT_QUESTIONNAIRE_PROMPT.format(
            offer_content=state.get("offer_content", "")[:8000]
        )
        result = self.llm_fast.complete_json(
            self._system(state), prompt,
            default={"has_questionnaire": False, "questions": []},
        )
        questions = result.get("questions", []) if result.get("has_questionnaire") else []
        has = bool(questions)
        logger.info(f"Questionnaire: {'yes' if has else 'no'} ({len(questions)} questions)")
        return {"questionnaire": questions, "has_questionnaire": has}

    # ── generate_responses ────────────────────────────────────────────────────

    def generate_responses(self, state: AgentState) -> dict:
        questions = state.get("questionnaire", [])
        logger.info(f"[node] generate_responses ({len(questions)} questions)")
        if not questions:
            return {"responses": []}

        import json as _json
        prompt = QUESTIONNAIRE_USER_PROMPT.format(
            offer_text=state.get("offer_content", "")[:4000],
            questions_json=_json.dumps(questions, ensure_ascii=False, indent=2),
        )
        responses = self.llm.complete_json(self._system(state), prompt, default=[])
        if not isinstance(responses, list):
            responses = []
        return {
            "responses": responses,
            "messages": [("assistant", f"generated {len(responses)} responses")],
        }

    # ── generate_recommendation ───────────────────────────────────────────────

    def generate_recommendation(self, state: AgentState) -> dict:
        logger.info("[node] generate_recommendation")
        import json as _json
        analysis = state.get("offer_analysis", {})
        prompt = RECOMMENDATION_PROMPT.format(
            analysis_json=_json.dumps(analysis, ensure_ascii=False, indent=2),
            offer_excerpt=state.get("offer_content", "")[:2000],
        )
        rec = self.llm.complete_json(
            self._system(state), prompt,
            default={"apply": analysis.get("score", 5) >= 6, "reasoning": "n/a"},
        )

        # Generate the actual cover letter text (plain, not JSON)
        try:
            letter = self.llm.complete(
                self._system(state),
                COVER_LETTER_USER_PROMPT.format(offer_text=state.get("offer_content", "")[:3000]),
            ).strip()
            rec["cover_letter"] = letter
        except Exception as e:
            logger.warning(f"Cover letter generation failed: {e}")
            rec["cover_letter"] = ""

        return {
            "recommendation": rec,
            "messages": [("assistant", f"recommendation: apply={rec.get('apply')}")],
        }

    # ── notify ────────────────────────────────────────────────────────────────

    def notify(self, state: AgentState) -> dict:
        logger.info("[node] notify")
        # Persist the analyzed offer to the jobs DB (dedup handled inside add_job)
        self._persist(state)

        if state.get("dry_run"):
            logger.info("[notify] dry-run — notification suppressed")
            return {"next_action": "done"}

        if self.notifier is None:
            logger.debug("[notify] no notifier configured")
            return {"next_action": "done"}

        try:
            self.notifier(state)  # callable injected by serve/analyze caller
        except Exception as e:
            logger.error(f"notify failed: {e}")
            return {"errors": [f"notify failed: {e}"], "next_action": "done"}
        return {"next_action": "done"}

    # ── error terminal ────────────────────────────────────────────────────────

    def handle_error(self, state: AgentState) -> dict:
        errs = state.get("errors", [])
        logger.error(f"[node] handle_error: {errs}")
        return {"next_action": "done"}

    # ── helpers ───────────────────────────────────────────────────────────────

    def _system(self, state: AgentState) -> str:
        return GRAPH_SYSTEM_PROMPT.format(context=state.get("context", self._profile_yaml))

    def _persist(self, state: AgentState) -> None:
        if not self.db:
            return
        meta = state.get("offer_meta", {})
        analysis = state.get("offer_analysis", {})
        try:
            job = {
                "source": "url_analyze",
                "title": meta.get("title", "")[:200] or "Oferta analizada",
                "company": meta.get("company", ""),
                "location": meta.get("location", ""),
                "salary_raw": meta.get("salary", ""),
                "url": meta.get("source_url", state.get("url", "")),
                "description": state.get("offer_content", "")[:4000],
                "fit_score": analysis.get("score"),
                "fit_reason": analysis.get("reasoning", ""),
            }
            is_new, job_id = self.db.add_job(job)
            responses = state.get("responses", [])
            if responses:
                self.db.save_responses(job_id, [
                    {"question": r.get("question", ""), "answer": r.get("answer", "")}
                    for r in responses
                ])
            logger.debug(f"persisted job id={job_id} (new={is_new})")
        except Exception as e:
            logger.warning(f"persist failed: {e}")
