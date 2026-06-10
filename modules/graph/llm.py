"""Thin LLM wrapper around Azure OpenAI (langchain-openai) with robust JSON parsing."""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from loguru import logger


def _resolve_env(value: str) -> str:
    """Expand a ${ENV_VAR} placeholder. Plain values pass through."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        resolved = os.environ.get(value[2:-1], "")
        if not resolved:
            raise ValueError(f"Environment variable {value[2:-1]} is not set")
        return resolved
    return value


def _env_or_setting(env_key: str, setting_val: str) -> str:
    """Prefer the raw environment variable; fall back to a settings value."""
    return os.environ.get(env_key) or _resolve_env(setting_val or "")


class LLM:
    """Wraps AzureChatOpenAI. Deployment (e.g. gpt-4o-mini) from env/settings.

    Reads AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT_NAME,
    AZURE_OPENAI_API_VERSION. `use_fast` is kept for API compat — Azure serves both
    tiers from the same deployment unless a separate `fast_deployment` is configured.
    """

    def __init__(self, settings: dict, use_fast: bool = False):
        from langchain_openai import AzureChatOpenAI

        a = settings.get("azure_openai", {})

        api_key = _env_or_setting("AZURE_OPENAI_API_KEY", a.get("api_key", ""))
        endpoint = _env_or_setting("AZURE_OPENAI_ENDPOINT", a.get("endpoint", ""))
        api_version = _env_or_setting("AZURE_OPENAI_API_VERSION", a.get("api_version", ""))
        deployment = _env_or_setting("AZURE_OPENAI_DEPLOYMENT_NAME", a.get("deployment", ""))
        if use_fast and a.get("fast_deployment"):
            deployment = _resolve_env(a["fast_deployment"])

        if not (api_key and endpoint and deployment):
            raise ValueError(
                "Missing Azure OpenAI config — set AZURE_OPENAI_API_KEY, "
                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT_NAME"
            )

        self.model = deployment
        self._client = AzureChatOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            azure_deployment=deployment,
            api_version=api_version or "2025-01-01-preview",
            max_tokens=a.get("max_tokens", 4096),
            temperature=a.get("temperature", 0.7),
        )

    # ── Calls ─────────────────────────────────────────────────────────────────

    def complete(self, system: str, user: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = self._client.invoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )
        return resp.content if isinstance(resp.content, str) else str(resp.content)

    def complete_json(self, system: str, user: str, default: Any = None) -> Any:
        """Call the model and parse a JSON object/array from the reply."""
        raw = self.complete(system, user)
        parsed = _parse_json(raw)
        if parsed is None:
            logger.warning(f"LLM returned non-JSON; using default. Head: {raw[:120]!r}")
            return default
        return parsed


def _parse_json(raw: str) -> Optional[Any]:
    text = raw.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        text = text.rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last resort: grab the outermost {...} or [...]
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = text.find(opener), text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
    return None
