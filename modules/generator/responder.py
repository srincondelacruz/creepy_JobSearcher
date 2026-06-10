"""Azure OpenAI generator for questionnaire responses, fit scores, and cover letters."""
import json
import os
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger

from .prompts import (
    SYSTEM_PROMPT,
    QUESTIONNAIRE_USER_PROMPT,
    FIT_SCORE_SYSTEM,
    FIT_SCORE_USER_PROMPT,
    COVER_LETTER_USER_PROMPT,
)


class Responder:
    def __init__(self, settings: dict, profile: dict, obsidian_notes: str = ""):
        from langchain_openai import AzureChatOpenAI

        a = settings.get("azure_openai", {})
        self.max_tokens = a.get("max_tokens", 2048)
        self.temperature = a.get("temperature", 0.7)

        api_key = self._env_or_setting("AZURE_OPENAI_API_KEY", a.get("api_key", ""))
        endpoint = self._env_or_setting("AZURE_OPENAI_ENDPOINT", a.get("endpoint", ""))
        api_version = self._env_or_setting("AZURE_OPENAI_API_VERSION", a.get("api_version", ""))
        deployment = self._env_or_setting("AZURE_OPENAI_DEPLOYMENT_NAME", a.get("deployment", ""))

        if not (api_key and endpoint and deployment):
            raise ValueError(
                "Missing Azure OpenAI config — set AZURE_OPENAI_API_KEY, "
                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT_NAME"
            )

        self.model = deployment
        self.client = AzureChatOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            azure_deployment=deployment,
            api_version=api_version or "2025-01-01-preview",
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

        self.profile = profile
        self.obsidian_notes = obsidian_notes
        self._profile_text = yaml.dump(profile, allow_unicode=True, default_flow_style=False)
        self._system = self._build_system_prompt()

    # ── Public interface ──────────────────────────────────────────────────────

    def generate_responses(
        self, offer_text: str, questions: list[str]
    ) -> list[dict]:
        """Return list of {question, answer, language} dicts."""
        questions_json = json.dumps(questions, ensure_ascii=False, indent=2)
        user_msg = QUESTIONNAIRE_USER_PROMPT.format(
            offer_text=offer_text[:3000],
            questions_json=questions_json,
        )
        raw = self._call(user_msg)
        return self._parse_json_list(raw, fallback_questions=questions)

    def score_job(self, title: str, company: str, description: str) -> dict:
        """Return fit score dict: {score, priority, strengths, gaps, reasoning, suggested_projects}."""
        profile_summary = self._build_profile_summary()
        system = FIT_SCORE_SYSTEM.format(profile_summary=profile_summary)
        user_msg = FIT_SCORE_USER_PROMPT.format(
            title=title,
            company=company,
            description=description[:3000],
        )
        raw = self._call(user_msg, system_override=system)
        result = self._parse_json_dict(raw)
        # Ensure score is int in range 1-10
        result["score"] = max(1, min(10, int(result.get("score", 5))))
        return result

    def generate_cover_letter(self, offer_text: str) -> str:
        """Return cover letter as plain text."""
        user_msg = COVER_LETTER_USER_PROMPT.format(offer_text=offer_text[:3000])
        return self._call(user_msg).strip()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _call(self, user_message: str, system_override: Optional[str] = None) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        system = system_override or self._system
        try:
            resp = self.client.invoke(
                [SystemMessage(content=system), HumanMessage(content=user_message)]
            )
            return resp.content if isinstance(resp.content, str) else str(resp.content)
        except Exception as e:
            logger.error(f"Azure OpenAI error: {e}")
            raise

    def _build_system_prompt(self) -> str:
        obsidian_section = (
            f"## ADDITIONAL CONTEXT (from Obsidian notes)\n{self.obsidian_notes}"
            if self.obsidian_notes
            else ""
        )
        return SYSTEM_PROMPT.format(
            profile=self._profile_text,
            obsidian_section=obsidian_section,
        )

    def _build_profile_summary(self) -> str:
        p = self.profile
        personal = p.get("personal", {})
        skills = p.get("skills", {})
        certs = [c["name"] for c in p.get("certifications", [])]
        all_skills = []
        for category in skills.values():
            if isinstance(category, list):
                all_skills.extend(
                    s["name"] if isinstance(s, dict) else s for s in category
                )
        return (
            f"Name: {personal.get('name')}\n"
            f"Location: {personal.get('location')}\n"
            f"Target roles: {', '.join(p.get('objectives', {}).get('roles', []))}\n"
            f"Key skills: {', '.join(all_skills[:20])}\n"
            f"Certifications: {', '.join(certs)}\n"
            f"Projects: {', '.join(proj['name'] for proj in p.get('projects', []))}\n"
        )

    @staticmethod
    def _parse_json_list(raw: str, fallback_questions: list[str]) -> list[dict]:
        """Parse JSON array from LLM response, with graceful fallback."""
        text = raw.strip()
        # Strip accidental markdown fences
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
            text = text.rstrip("`").strip()
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
        # Fallback: wrap raw text as single answer
        logger.warning("Could not parse JSON list from LLM — returning raw text")
        return [{"question": q, "answer": raw, "language": "es"} for q in fallback_questions[:1]]

    @staticmethod
    def _parse_json_dict(raw: str) -> dict:
        text = raw.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
            text = text.rstrip("`").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Could not parse JSON dict from LLM")
            return {"score": 5, "priority": "medium", "reasoning": raw[:200]}

    @staticmethod
    def _resolve_env(value: str) -> str:
        """Expand ${ENV_VAR} placeholders."""
        if value.startswith("${") and value.endswith("}"):
            var = value[2:-1]
            resolved = os.environ.get(var, "")
            if not resolved:
                raise ValueError(f"Environment variable {var} is not set")
            return resolved
        return value

    @staticmethod
    def _env_or_setting(env_key: str, setting_val: str) -> str:
        """Prefer the raw environment variable; fall back to a settings value."""
        return os.environ.get(env_key) or Responder._resolve_env(setting_val or "")


def load_responder(settings: dict, profile: dict, obsidian_notes: str = "") -> Responder:
    return Responder(settings, profile, obsidian_notes)
