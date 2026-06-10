"""Render a final AgentState into human-readable output (terminal + Telegram chunks)."""
from __future__ import annotations

from modules.graph.state import AgentState

_TG_LIMIT = 3800  # stay under Telegram's 4096 hard limit with margin


def format_telegram(state: AgentState) -> list[str]:
    """Return a list of message chunks ready to send via Telegram (Markdown)."""
    if state.get("errors") and not state.get("offer_analysis"):
        return [f"❌ *No pude analizar la oferta*\n\n{'; '.join(state['errors'])}"]

    meta = state.get("offer_meta", {})
    a = state.get("offer_analysis", {})
    rec = state.get("recommendation", {})

    score = a.get("score", "?")
    emoji = _score_emoji(score)
    header = (
        f"{emoji} *Análisis de oferta — {score}/10*\n"
        f"📋 {meta.get('title') or a.get('role_summary', 'Oferta')}\n"
    )
    if meta.get("company"):
        header += f"🏢 {meta['company']}\n"
    if meta.get("location"):
        header += f"📍 {meta['location']}\n"
    if meta.get("salary"):
        header += f"💶 {meta['salary']}\n"
    header += f"\n🔗 {meta.get('source_url', state.get('url', ''))}"

    blocks = [header]

    # Strengths / weaknesses
    body = ""
    if a.get("strengths"):
        body += "\n*✅ Puntos fuertes:*\n" + "\n".join(f"• {s}" for s in a["strengths"][:5])
    if a.get("weaknesses"):
        body += "\n\n*⚠️ Puntos débiles:*\n" + "\n".join(f"• {w}" for w in a["weaknesses"][:5])
    if a.get("matched_tech"):
        body += "\n\n*🟢 Tecnologías que dominas:* " + ", ".join(a["matched_tech"][:12])
    if a.get("missing_tech"):
        body += "\n*🔴 Tecnologías que faltan:* " + ", ".join(a["missing_tech"][:12])
    if body:
        blocks.append(body.strip())

    # Objections
    if a.get("objections"):
        obj = "*🛡 Posibles objeciones del recruiter:*\n"
        for o in a["objections"][:4]:
            obj += f"\n_{o.get('objection', '')}_\n→ {o.get('rebuttal', '')}\n"
        blocks.append(obj.strip())

    # Recommendation
    if rec:
        apply = rec.get("apply")
        verdict = "✅ *APLICAR*" if apply else "🚫 *No aplicar*"
        recblock = f"{verdict} (confianza: {rec.get('confidence', '?')})\n{rec.get('reasoning', '')}"
        if rec.get("cv_tips"):
            recblock += "\n\n*📝 Ajustes de CV:*\n" + "\n".join(f"• {t}" for t in rec["cv_tips"][:5])
        if rec.get("cover_letter_angle"):
            recblock += f"\n\n*✉️ Enfoque carta:* {rec['cover_letter_angle']}"
        blocks.append(recblock)

    # Questionnaire responses
    responses = state.get("responses", [])
    if responses:
        blocks.append(f"*📨 Respuestas al cuestionario ({len(responses)}):*")
        for r in responses:
            blocks.append(f"*❓ {r.get('question', '')}*\n\n{r.get('answer', '')}")

    # Cover letter (last, often long)
    if rec.get("cover_letter"):
        blocks.append("*✉️ Carta de presentación:*\n\n" + rec["cover_letter"])

    return _chunk(blocks)


def format_terminal(state: AgentState) -> str:
    """Plain rich-markup string for terminal display."""
    if state.get("errors") and not state.get("offer_analysis"):
        return f"[red]Error:[/red] {'; '.join(state['errors'])}"

    meta = state.get("offer_meta", {})
    a = state.get("offer_analysis", {})
    rec = state.get("recommendation", {})
    lines = []

    score = a.get("score", "?")
    lines.append(f"[bold]━━ {meta.get('title') or 'Oferta'} ━━[/bold]")
    if meta.get("company"):
        lines.append(f"Empresa: {meta['company']}")
    lines.append(f"[bold cyan]Encaje: {score}/10[/bold cyan] ({a.get('priority', '?')})")
    if a.get("reasoning"):
        lines.append(f"\n{a['reasoning']}")

    if a.get("matched_tech"):
        lines.append(f"\n[green]Dominas:[/green] {', '.join(a['matched_tech'])}")
    if a.get("missing_tech"):
        lines.append(f"[red]Faltan:[/red] {', '.join(a['missing_tech'])}")

    if a.get("strengths"):
        lines.append("\n[green]Puntos fuertes:[/green]")
        lines += [f"  ✓ {s}" for s in a["strengths"]]
    if a.get("weaknesses"):
        lines.append("\n[yellow]Puntos débiles:[/yellow]")
        lines += [f"  △ {w}" for w in a["weaknesses"]]

    if a.get("objections"):
        lines.append("\n[magenta]Objeciones y respuestas:[/magenta]")
        for o in a["objections"]:
            lines.append(f"  • {o.get('objection', '')}")
            lines.append(f"    → {o.get('rebuttal', '')}")

    responses = state.get("responses", [])
    if responses:
        lines.append("\n[bold]Respuestas al cuestionario:[/bold]")
        for i, r in enumerate(responses, 1):
            lines.append(f"\n[cyan]Q{i}. {r.get('question', '')}[/cyan]")
            lines.append(r.get("answer", ""))

    if rec:
        verdict = "[green]APLICAR[/green]" if rec.get("apply") else "[red]No aplicar[/red]"
        lines.append(f"\n[bold]Recomendación:[/bold] {verdict} — {rec.get('reasoning', '')}")
        if rec.get("cv_tips"):
            lines.append("[blue]Ajustes de CV:[/blue]")
            lines += [f"  → {t}" for t in rec["cv_tips"]]
        if rec.get("cover_letter"):
            lines.append("\n[bold]━━ Carta de presentación ━━[/bold]\n")
            lines.append(rec["cover_letter"])

    return "\n".join(lines)


# ── helpers ───────────────────────────────────────────────────────────────────

def _score_emoji(score) -> str:
    try:
        s = int(score)
    except (ValueError, TypeError):
        return "📊"
    if s >= 8:
        return "🟢"
    if s >= 6:
        return "🟡"
    return "🔴"


def _chunk(blocks: list[str]) -> list[str]:
    """Pack blocks into Telegram-sized messages; split oversized blocks."""
    out: list[str] = []
    buf = ""
    for block in blocks:
        if len(block) > _TG_LIMIT:
            if buf:
                out.append(buf)
                buf = ""
            # hard-split the long block
            for i in range(0, len(block), _TG_LIMIT):
                out.append(block[i : i + _TG_LIMIT])
            continue
        if len(buf) + len(block) + 2 > _TG_LIMIT:
            out.append(buf)
            buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        out.append(buf)
    return out
