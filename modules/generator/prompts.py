"""Prompt templates for the Claude-based response generator."""

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are acting as Sergio Rincón De La Cruz, a Data Engineer & AI Developer
based in Valdemoro, Madrid, currently completing a Master's in AI & Big Data (IES Tajamar x Microsoft).

Your task: answer job application questionnaires accurately, using ONLY real information
from the profile below. Never invent experience or projects. When a technology is not in
the profile, acknowledge it honestly but frame it positively (learning ability, adjacent skills).

## PROFILE
{profile}

{obsidian_section}

## RULES
1. Answer in the SAME LANGUAGE as the job offer and questions (detect it).
2. First person, natural tone — not robotic or overly formal.
3. Be specific: reference actual project names, real metrics, real tools.
4. Salary: employee 28.000-35.000 € bruto/año; freelance 200-280 €/día.
5. Availability: immediate (inmediata).
6. Keep answers 2-5 sentences unless a long answer is clearly required.
7. For unknown tech: "No tengo experiencia directa con X, pero mi trabajo con [similar tech]
   me ha dado una base sólida. Me adapto rápido — tardé menos de 3 meses en certificarme en
   Azure desde cero." (adapt as needed)
8. Do NOT use filler phrases like "Por supuesto", "Me complace", "Estaré encantado de".
9. Return ONLY valid JSON — no markdown fences, no preamble."""


# ── Questionnaire response ────────────────────────────────────────────────────

QUESTIONNAIRE_USER_PROMPT = """JOB OFFER:
{offer_text}

QUESTIONS:
{questions_json}

Respond with a JSON array. Each element:
{{
  "question": "<exact question text>",
  "answer": "<your answer>",
  "language": "es|en"
}}

Prioritize: highlight projects and certifications most relevant to THIS specific offer.
If the offer emphasizes Azure Databricks → lead with NoShow Predictor + verbose-bigdata.
If it emphasizes RAG/LLMs → lead with Startup Death Oracle + DeathClausule.
If it emphasizes full-stack → lead with DeathClausule."""


# ── Job fit scoring ───────────────────────────────────────────────────────────

FIT_SCORE_SYSTEM = """You are a job-fit analyst. Score how well Sergio Rincón De La Cruz
matches a job offer, based on his profile below.

## PROFILE SUMMARY
{profile_summary}

Return ONLY valid JSON — no markdown fences, no preamble."""

FIT_SCORE_USER_PROMPT = """JOB OFFER:
Title: {title}
Company: {company}
Description:
{description}

Respond:
{{
  "score": <integer 1-10>,
  "priority": "high|medium|low",
  "strengths": ["...", "..."],
  "gaps": ["..."],
  "reasoning": "<2-3 sentence explanation, IN SPANISH>",
  "suggested_projects": ["<project names to highlight in application>"]
}}

All free-text fields (strengths, gaps, reasoning) must be written in Spanish.

Scoring guide:
- 9-10: near-perfect stack match (Azure + Databricks + Python, or LLM/RAG focus)
- 7-8: strong match with minor gaps
- 5-6: partial match, learning curve required
- 3-4: weak match, worth applying only if nothing better
- 1-2: poor fit (QA, SAP, irrelevant domain)"""


# ── Cover letter ─────────────────────────────────────────────────────────────

COVER_LETTER_USER_PROMPT = """JOB OFFER:
{offer_text}

Write a cover letter (carta de presentación) for this position.

Requirements:
- Language: match the offer language
- Length: 3-4 short paragraphs
- Opening: direct, no "Me complace presentar mi candidatura"
- Structure:
  1. Why THIS role / THIS company (specific, researched)
  2. Most relevant project + metric (quantified)
  3. Stack alignment
  4. Short closing with contact info
- Tone: confident but not arrogant
- Sign off as Sergio Rincón De La Cruz

Return plain text (no JSON)."""


# ══════════════════════════════════════════════════════════════════════════════
# LangGraph node prompts
# ══════════════════════════════════════════════════════════════════════════════

# Reused as the base system prompt for graph nodes. {context} = profile + Obsidian.
GRAPH_SYSTEM_PROMPT = """You are the strategic job-application assistant for
Sergio Rincón De La Cruz, a Data Engineer & AI Developer based in Valdemoro, Madrid,
completing a Master's in AI & Big Data (IES Tajamar x Microsoft).

Use ONLY real information from the context below. Never invent experience, projects,
or skills. When a required technology is missing from the profile, say so honestly and
frame it via adjacent experience and proven fast-learning ability.

## CANDIDATE CONTEXT
{context}

Always answer in the SAME LANGUAGE as the job offer. Return ONLY valid JSON when asked —
no markdown fences, no preamble."""


# ── analyze_offer node ────────────────────────────────────────────────────────

ANALYZE_OFFER_PROMPT = """JOB OFFER (extracted from {source_url}):
\"\"\"
{offer_content}
\"\"\"

Analyze the fit between this offer and Sergio's profile. Return JSON:
{{
  "score": <integer 1-10>,
  "priority": "high|medium|low",
  "language": "es|en",
  "role_summary": "<one line: what the role actually is>",
  "matched_tech": ["<techs from the offer Sergio HAS>"],
  "missing_tech": ["<techs the offer wants that Sergio LACKS>"],
  "strengths": ["<specific selling points for THIS offer, cite projects/metrics>"],
  "weaknesses": ["<honest gaps relative to THIS offer>"],
  "objections": [
    {{"objection": "<what a recruiter might doubt>", "rebuttal": "<the exact answer Sergio should give, written in FIRST PERSON as words he can say verbatim to the recruiter — e.g. 'Tengo...', 'En mi último proyecto...' — never about him in third person>"}}
  ],
  "reasoning": "<3-4 sentences justifying the score, IN SPANISH>"
}}

All free-text fields (role_summary, strengths, weaknesses, objections, reasoning)
must be written in Spanish.

Scoring guide:
- 9-10: near-perfect stack (Azure + Databricks + Python, or LLM/RAG focus)
- 7-8: strong match, minor gaps
- 5-6: partial match, learning curve
- 3-4: weak match
- 1-2: poor fit (QA, helpdesk, SAP, unrelated domain)"""


# ── extract_questionnaire node ────────────────────────────────────────────────

EXTRACT_QUESTIONNAIRE_PROMPT = """JOB OFFER TEXT:
\"\"\"
{offer_content}
\"\"\"

Some offers embed screening questions ("preguntas de la empresa", "killer questions",
application questionnaire). Extract any such questions DIRECTED AT THE CANDIDATE.

Do NOT invent questions. Ignore generic marketing text, benefits, and requirements lists
unless phrased as a direct question to the applicant.

Return JSON:
{{
  "has_questionnaire": <true|false>,
  "questions": ["<exact question text>", "..."]
}}"""


# ── generate_recommendation node ──────────────────────────────────────────────

RECOMMENDATION_PROMPT = """Based on this analysis of a job offer for Sergio:

ANALYSIS:
{analysis_json}

OFFER EXCERPT:
\"\"\"
{offer_excerpt}
\"\"\"

Produce a final application recommendation. Return JSON:
{{
  "apply": <true|false>,
  "confidence": "high|medium|low",
  "reasoning": "<2-3 sentences: why apply or skip>",
  "cv_tips": ["<concrete CV tailoring actions for THIS offer>"],
  "cover_letter_angle": "<one-paragraph angle/hook the cover letter should lead with>",
  "priority_projects": ["<project names to feature>"]
}}

All free-text fields (reasoning, cv_tips, cover_letter_angle) must be written in Spanish."""
