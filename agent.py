#!/usr/bin/env python3
"""Job Agent CLI — entry point for all commands."""
import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click
from loguru import logger
from rich.console import Console
from rich.table import Table
from rich import box

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

console = Console()


def _load_all(base_dir: Path):
    """Load settings, profile, keywords, database, and responder."""
    from modules.utils import load_settings, load_profile, load_keywords, setup_logging
    from modules.storage.database import Database
    from modules.generator.responder import Responder

    settings = load_settings(base_dir)
    setup_logging(settings, base_dir)
    profile, obsidian_notes = load_profile(base_dir, settings)
    keywords = load_keywords(base_dir)
    db_path = base_dir / settings.get("storage", {}).get("db_path", "data/jobs.db")
    db = Database(str(db_path))
    responder = Responder(settings, profile, obsidian_notes)
    return settings, profile, keywords, db, responder


def _build_agent_graph(base_dir: Path, settings, profile, db, notifier=None):
    """Construct the LangGraph agent with Obsidian memory + URL fetcher."""
    from modules.memory.obsidian_memory import ObsidianMemory
    from modules.scraper.url_fetcher import URLFetcher
    from modules.graph.agent_graph import JobAgentGraph

    obsidian = ObsidianMemory(settings)
    fetcher = URLFetcher(settings)
    return JobAgentGraph(settings, profile, obsidian, fetcher, db, base_dir, notifier=notifier)


def _read_input(value: str) -> str:
    """If value is a file path, read it; otherwise return as-is."""
    p = Path(value)
    if p.exists() and p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return value.strip()


# ── CLI root ──────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Job Agent — automated job search and application assistant for Sergio Rincón."""
    pass


# ── respond ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--offer", "-o", required=True, help="Job offer text or path to .txt file")
@click.option("--questions", "-q", required=True,
              help="Questions separated by '|', newlines, or path to file")
@click.option("--output", "-O", default=None, help="Save output to file (markdown)")
@click.option("--json-output", is_flag=True, default=False, help="Output raw JSON")
@click.option("--cover-letter", is_flag=True, default=False, help="Also generate cover letter")
def respond(offer, questions, output, json_output, cover_letter):
    """Generate questionnaire responses for a job offer."""
    settings, profile, keywords, db, responder = _load_all(BASE_DIR)

    offer_text = _read_input(offer)
    questions_raw = _read_input(questions)
    question_list = [q.strip() for q in questions_raw.replace("\n", "|").split("|") if q.strip()]

    if not question_list:
        console.print("[red]No questions found. Separate with '|' or newlines.[/red]")
        sys.exit(1)

    console.print(f"[cyan]Generating responses for {len(question_list)} question(s)...[/cyan]")

    try:
        responses = responder.generate_responses(offer_text, question_list)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    if json_output:
        print(json.dumps(responses, ensure_ascii=False, indent=2))
    else:
        _print_responses(responses)

    if cover_letter:
        console.print("\n[cyan]Generating cover letter...[/cyan]")
        try:
            letter = responder.generate_cover_letter(offer_text)
            console.print("\n[bold]━━ COVER LETTER ━━[/bold]\n")
            console.print(letter)
            if output:
                _append_to_file(output, "\n\n---\n## Cover Letter\n\n" + letter)
        except Exception as e:
            console.print(f"[red]Cover letter error: {e}[/red]")

    if output:
        _save_responses_to_file(output, offer_text, responses)
        console.print(f"\n[green]Saved to {output}[/green]")


# ── cover-letter ──────────────────────────────────────────────────────────────

@cli.command("cover-letter")
@click.option("--offer", "-o", required=True, help="Job offer text or .txt file path")
@click.option("--output", "-O", default=None, help="Save to file")
def cover_letter(offer, output):
    """Generate a cover letter for a job offer."""
    settings, profile, keywords, db, responder = _load_all(BASE_DIR)
    offer_text = _read_input(offer)
    console.print("[cyan]Generating cover letter...[/cyan]")
    try:
        letter = responder.generate_cover_letter(offer_text)
        console.print(letter)
        if output:
            Path(output).write_text(letter, encoding="utf-8")
            console.print(f"\n[green]Saved to {output}[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


# ── score ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--offer", "-o", required=True, help="Job offer text or .txt file path")
@click.option("--title", "-t", default="", help="Job title (optional override)")
@click.option("--company", "-c", default="", help="Company name (optional)")
def score(offer, title, company):
    """Score job fit (1-10) for a given offer."""
    settings, profile, keywords, db, responder = _load_all(BASE_DIR)
    offer_text = _read_input(offer)
    console.print("[cyan]Scoring job fit...[/cyan]")
    try:
        result = responder.score_job(title or "Unknown", company or "Unknown", offer_text)
        _print_score(result)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


# ── search ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--dry-run", is_flag=True, default=False, help="Don't save or notify, just print")
@click.option("--no-score", is_flag=True, default=False, help="Skip fit scoring (faster)")
@click.option("--keyword", "-k", default=None, help="Override keywords (single search term)")
@click.option("--source", "-s", default=None, help="infojobs|tecnoempleo (default: all)")
def search(dry_run, no_score, keyword, source):
    """Run a manual job search across configured sources."""
    settings, profile, keywords_cfg, db, responder = _load_all(BASE_DIR)
    new_jobs, warnings = _run_search(settings, profile, keywords_cfg, db, responder,
                                     dry_run=dry_run, score_jobs=not no_score,
                                     keyword_override=keyword, source_override=source)
    for w in warnings:
        console.print(f"[red]⚠ {w}[/red]")


# ── analyze ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--url", "-u", required=True, help="Job offer URL to analyze")
@click.option("--dry-run", is_flag=True, default=False,
              help="Run the graph but don't notify or persist side effects")
@click.option("--json-output", is_flag=True, default=False, help="Print full state as JSON")
@click.option("--output", "-O", default=None, help="Save markdown report to file")
def analyze(url, dry_run, json_output, output):
    """Analyze a single job offer by URL via the LangGraph agent."""
    settings, profile, keywords_cfg, db, responder = _load_all(BASE_DIR)
    console.print(f"[cyan]Running agent graph on:[/cyan] {url}")

    try:
        agent = _build_agent_graph(BASE_DIR, settings, profile, db)
        state = agent.analyze_url(url, dry_run=dry_run)
    except Exception as e:
        console.print(f"[red]Graph error: {e}[/red]")
        sys.exit(1)

    if json_output:
        # state may contain non-serializable message objects — keep the data fields
        clean = {k: v for k, v in state.items() if k != "messages"}
        print(json.dumps(clean, ensure_ascii=False, indent=2, default=str))
    else:
        from modules.graph.formatting import format_terminal
        console.print(format_terminal(state))

    if output:
        from modules.graph.formatting import format_terminal
        import re as _re
        plain = _re.sub(r"\[/?[a-z ]+\]", "", format_terminal(state))  # strip rich tags
        Path(output).write_text(plain, encoding="utf-8")
        console.print(f"\n[green]Saved report to {output}[/green]")


# ── serve ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--dry-run", is_flag=True, default=False,
              help="No real notifications, no DB writes")
def serve(dry_run):
    """Start Telegram bot + APScheduler background search + LangGraph /analizar."""
    settings, profile, keywords_cfg, db, responder = _load_all(BASE_DIR)

    import datetime as dt

    from modules.notifier.telegram_bot import JobBot
    import pytz

    # Build the LangGraph agent so /analizar works inside the bot
    try:
        agent_graph = _build_agent_graph(BASE_DIR, settings, profile, db)
    except Exception as e:
        logger.warning(f"Agent graph unavailable (/analizar disabled): {e}")
        agent_graph = None

    bot = JobBot(settings, db, responder, dry_run=dry_run, agent_graph=agent_graph)

    sched_cfg = settings.get("scheduler", {})
    if sched_cfg.get("enabled", True):
        tz = pytz.timezone(sched_cfg.get("timezone", "Europe/Madrid"))
        time_str = sched_cfg.get("search_time", "09:00")
        hour, minute = map(int, time_str.split(":"))

        bot.schedule_daily_search(
            lambda: _run_search(settings, profile, keywords_cfg, db, responder,
                                dry_run=dry_run, score_jobs=True),
            at=dt.time(hour, minute, tzinfo=tz),
        )
        console.print(f"[green]Scheduler active — daily search at {time_str} {tz}[/green]")

    if dry_run:
        console.print("[yellow]DRY-RUN mode — notifications suppressed[/yellow]")

    console.print("[green]Starting Telegram bot...[/green]")
    bot.run()


# ── status ────────────────────────────────────────────────────────────────────

@cli.command()
def status():
    """Show job database stats and recent top matches."""
    _, _, _, db, _ = _load_all(BASE_DIR)
    stats = db.get_stats()

    table = Table(title="Job Agent — Status", box=box.ROUNDED, show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Total offers", str(stats["total"]))
    for s, count in stats.get("by_status", {}).items():
        table.add_row(f"  ↳ {s}", str(count))
    table.add_row("", "")
    for src, count in stats.get("by_source", {}).items():
        table.add_row(f"Source: {src}", str(count))
    if stats.get("avg_score"):
        table.add_row("Avg fit score", f"{stats['avg_score']}/10")
    console.print(table)

    if stats.get("top_jobs"):
        top_table = Table(title="Top Matches", box=box.SIMPLE)
        top_table.add_column("Score", style="green", width=6)
        top_table.add_column("Title")
        top_table.add_column("Company")
        top_table.add_column("URL", style="blue")
        for j in stats["top_jobs"]:
            top_table.add_row(
                str(j.get("fit_score", "?")),
                j["title"][:50],
                j["company"][:30],
                (j.get("url") or "")[:60],
            )
        console.print(top_table)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run_search(settings, profile, keywords_cfg, db, responder,
                dry_run=False, score_jobs=True,
                keyword_override=None, source_override=None) -> tuple[list[dict], list[str]]:
    """Run all scrapers. Returns (new_jobs, health_warnings).

    A scraper that yields 0 raw listings across every keyword is almost
    certainly broken (site redesign, ban) — that produces a warning so the
    failure is loud instead of looking like a quiet day.
    """
    from modules.scraper.infojobs import InfojobsScraper
    from modules.scraper.tecnoempleo import TecnoempleoScraper
    from modules.scraper.joinrs import JoinrsScraper
    from modules.utils import should_exclude

    scraping_cfg = settings.get("scraping", {})
    sources_cfg = keywords_cfg.get("sources", {})

    # Build scraper list
    scrapers = []
    if source_override in (None, "infojobs") and sources_cfg.get("infojobs", {}).get("enabled", True):
        scrapers.append(InfojobsScraper(scraping_cfg))
    if source_override in (None, "tecnoempleo") and sources_cfg.get("tecnoempleo", {}).get("enabled", True):
        scrapers.append(TecnoempleoScraper(scraping_cfg))
    if source_override in (None, "joinrs") and sources_cfg.get("joinrs", {}).get("enabled", True):
        import os
        if os.getenv("JOINRS_EMAIL") and os.getenv("JOINRS_PASSWORD"):
            scrapers.append(JoinrsScraper(scraping_cfg))
        else:
            logger.debug("[Joinrs] Skipped — JOINRS_EMAIL / JOINRS_PASSWORD not set")

    if not scrapers:
        logger.warning("No scrapers enabled")
        return [], ["No hay scrapers habilitados en config/keywords.yaml"]

    kw_cfg = keywords_cfg.get("search_keywords", {})
    if keyword_override:
        search_terms = [keyword_override]
    else:
        search_terms = (
            kw_cfg.get("primary", [])
            + kw_cfg.get("junior", [])
        )

    def _run_one_scraper(scraper) -> tuple[list[dict], int]:
        """Search all keywords on one scraper; return (new jobs, raw listing count)."""
        found: list[dict] = []
        raw_count = 0
        for keyword in search_terms:
            try:
                listings = scraper.search(keyword, "Madrid")
                raw_count += len(listings)
                for listing in listings:
                    job = listing.to_dict()

                    # Filter
                    if should_exclude(job["title"], job.get("salary_min"), keywords_cfg):
                        continue

                    # Skip already-known jobs BEFORE expensive detail fetch + LLM scoring
                    if not dry_run and db.has_job(job.get("url", ""), job["title"], job.get("company", "")):
                        continue

                    # Fetch details if description is empty
                    if not job.get("description") and listing.url:
                        try:
                            listing = scraper.get_details(listing)
                            job = listing.to_dict()
                        except Exception as e:
                            logger.debug(f"Could not fetch details for {listing.url}: {e}")

                    # Fit score
                    if score_jobs and job.get("description"):
                        try:
                            score_result = responder.score_job(
                                job["title"], job["company"], job["description"]
                            )
                            job["fit_score"] = score_result["score"]
                            job["fit_reason"] = score_result.get("reasoning", "")
                        except Exception as e:
                            logger.warning(f"Scoring failed for {job['title']}: {e}")

                    if dry_run:
                        _print_job_dry_run(job)
                        continue

                    is_new, job_id = db.add_job(job)
                    if is_new:
                        job["id"] = job_id
                        found.append(job)
                        logger.info(
                            f"New job: [{job.get('fit_score', '?')}/10] "
                            f"{job['title']} @ {job['company']}"
                        )
            except Exception as e:
                logger.error(f"Search error ({scraper.SOURCE}, {keyword!r}): {e}")
        return found, raw_count

    # Scrapers hit different domains, so running them in parallel keeps
    # per-domain anti-ban delays intact while halving total wall time.
    new_jobs: list[dict] = []
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=len(scrapers)) as pool:
        for scraper, (found, raw_count) in zip(scrapers, pool.map(_run_one_scraper, scrapers)):
            new_jobs.extend(found)
            if raw_count == 0:
                msg = (
                    f"{scraper.SOURCE} devolvió 0 resultados en {len(search_terms)} "
                    f"búsquedas — scraper posiblemente roto (rediseño web o bloqueo)"
                )
                logger.warning(msg)
                warnings.append(msg)

    if not dry_run:
        logger.info(f"Search complete — {len(new_jobs)} new jobs added")
    return new_jobs, warnings


def _print_responses(responses: list[dict]) -> None:
    for i, r in enumerate(responses, 1):
        console.print(f"\n[bold cyan]Q{i}. {r['question']}[/bold cyan]")
        console.print(f"[white]{r['answer']}[/white]")


def _print_score(result: dict) -> None:
    score = result.get("score", "?")
    priority = result.get("priority", "?")
    color = {"high": "green", "medium": "yellow", "low": "red"}.get(priority, "white")
    console.print(f"\n[bold {color}]Score: {score}/10 ({priority})[/bold {color}]")
    console.print(f"\n[cyan]Reasoning:[/cyan] {result.get('reasoning', '')}")
    if result.get("strengths"):
        console.print("\n[green]Strengths:[/green]")
        for s in result["strengths"]:
            console.print(f"  ✓ {s}")
    if result.get("gaps"):
        console.print("\n[yellow]Gaps:[/yellow]")
        for g in result["gaps"]:
            console.print(f"  △ {g}")
    if result.get("suggested_projects"):
        console.print("\n[blue]Highlight these projects:[/blue]")
        for p in result["suggested_projects"]:
            console.print(f"  → {p}")


def _print_job_dry_run(job: dict) -> None:
    score = job.get("fit_score", "?")
    console.print(
        f"[DRY-RUN] [{score}/10] {job['title']} @ {job['company']} "
        f"| {job.get('salary_raw') or 'no salary'} "
        f"| {job.get('url', '')[:60]}"
    )


def _save_responses_to_file(path: str, offer_text: str, responses: list[dict]) -> None:
    lines = ["# Job Application Responses\n", f"**Offer excerpt:** {offer_text[:300]}...\n\n---\n"]
    for i, r in enumerate(responses, 1):
        lines.append(f"## Q{i}. {r['question']}\n\n{r['answer']}\n")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def _append_to_file(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


if __name__ == "__main__":
    cli()
