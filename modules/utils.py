"""Shared utilities: config loading, profile loading, Obsidian integration."""
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger


def load_yaml(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _expand_env(obj):
    """Recursively expand ${ENV_VAR} in strings within a dict/list."""
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str) and "${" in obj:
        def replacer(m):
            return os.environ.get(m.group(1), m.group(0))
        return re.sub(r"\$\{([^}]+)\}", replacer, obj)
    return obj


def load_settings(base_dir: Path) -> dict:
    raw = load_yaml(base_dir / "config" / "settings.yaml")
    return _expand_env(raw)


def load_profile(base_dir: Path, settings: dict) -> tuple[dict, str]:
    """Return (profile_dict, obsidian_notes_str).

    If obsidian_profile_path is configured and the Obsidian file is newer
    than profile.yaml, read additional context from the markdown note.
    """
    profile_path = base_dir / "config" / "profile.yaml"
    profile = load_yaml(profile_path)

    obsidian_notes = ""
    obsidian_path_str = settings.get("obsidian_profile_path", "")
    if obsidian_path_str:
        obsidian_path = Path(obsidian_path_str)
        if obsidian_path.exists():
            profile_mtime = profile_path.stat().st_mtime
            obs_mtime = obsidian_path.stat().st_mtime
            with open(obsidian_path, "r", encoding="utf-8") as f:
                obs_content = f.read()
            obsidian_notes = _strip_frontmatter(obs_content)
            if obs_mtime > profile_mtime:
                logger.info(
                    f"Obsidian note is newer than profile.yaml — using it as primary context"
                )
            else:
                logger.debug("Obsidian note found but profile.yaml is more recent")
        else:
            logger.debug(f"obsidian_profile_path set but file not found: {obsidian_path_str}")

    return profile, obsidian_notes


def _strip_frontmatter(md_text: str) -> str:
    """Remove YAML frontmatter (--- ... ---) from Obsidian markdown."""
    stripped = md_text.strip()
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            return stripped[end + 3:].strip()
    return stripped


def load_keywords(base_dir: Path) -> dict:
    return load_yaml(base_dir / "config" / "keywords.yaml")


def format_salary(min_val: Optional[float], max_val: Optional[float], raw: str = "") -> str:
    if min_val and max_val:
        return f"{int(min_val):,} – {int(max_val):,} €"
    if min_val:
        return f"desde {int(min_val):,} €"
    return raw or "No indicado"


def should_exclude(title: str, salary_min: Optional[float], keywords_cfg: dict) -> bool:
    """Return True if this job should be filtered out."""
    title_lower = title.lower()
    for excl in keywords_cfg.get("exclude_keywords", []):
        if excl.lower() in title_lower:
            logger.debug(f"Excluded '{title}' — matched exclusion keyword '{excl}'")
            return True
    cfg_min_salary = keywords_cfg.get("min_salary_eur", 0)
    if salary_min is not None and salary_min < cfg_min_salary:
        logger.debug(f"Excluded '{title}' — salary {salary_min} < min {cfg_min_salary}")
        return True
    return False


def setup_logging(settings: dict, base_dir: Path) -> None:
    from loguru import logger
    import sys
    log_cfg = settings.get("logging", {})
    level = log_cfg.get("level", "INFO")
    log_file = base_dir / log_cfg.get("file", "logs/agent.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=level, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add(
        str(log_file),
        level=level,
        rotation=log_cfg.get("rotation", "10 MB"),
        retention=log_cfg.get("retention", "30 days"),
        encoding="utf-8",
    )
