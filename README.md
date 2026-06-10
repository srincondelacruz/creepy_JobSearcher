# Job Agent — Sergio Rincón De La Cruz

LangGraph-powered job-application agent. Analyzes any offer by URL, scores fit against your
profile (enriched live from your Obsidian vault), extracts and answers screening
questionnaires, and recommends whether to apply — all driven by **Azure OpenAI (GPT-4o mini)**.

## Features

- **🧠 LangGraph agent** — stateful graph with persistent cross-session memory (SqliteSaver)
- **🔗 On-demand URL analysis** — `analyze --url ...` runs the whole pipeline autonomously
- **📊 Deep fit analysis** — score 1-10, matched vs missing tech, strengths, weaknesses,
  recruiter objections + rebuttals
- **📝 Auto questionnaire** — detects embedded screening questions and answers them
- **🎯 Apply/skip recommendation** — with CV tailoring tips + cover letter
- **🗂 Obsidian memory** — recursively scans your vault (1h TTL cache), injects live context
- **🤖 Telegram bot** — `/analizar <url>` runs the graph and returns formatted results
- **📅 Daily scheduler** — 9:00 AM background search (legacy Infojobs/Tecnoempleo scraper)
- **🧪 Dry-run mode** — test the full graph without notifications or side effects

---

## Architecture — the Graph

```
                              START
                                │
                                ▼
                          load_context        ← profile.yaml + Obsidian vault
                                │
                                ▼
                          fetch_offer          ← Playwright → requests fallback
                                │
                            (router)
                    ┌───────────┴───────────┐
                  error                    ok
                    │                       │
                    ▼                       ▼
              handle_error            analyze_offer   ← score, tech, objections (GPT-4o mini)
                    │                       │
                    │                       ▼
                    │               extract_questionnaire  ← screening questions? (GPT-4o mini)
                    │                       │
                    │                   (router)
                    │              ┌────────┴────────┐
                    │       has questions       no questions
                    │              │                 │
                    │              ▼                 │
                    │     generate_responses         │
                    │              │                 │
                    │              └────────┬────────┘
                    │                       ▼
                    │            generate_recommendation  ← apply?, CV tips, cover letter
                    │                       │
                    │                       ▼
                    │                    notify          ← Telegram + persist to SQLite
                    │                       │
                    └───────────────────────┴──► END
```

State (`modules/graph/state.py`) flows between nodes; `MemorySaver`/`SqliteSaver`
checkpoints each run under a `thread_id`, so the agent remembers analyzed offers and
conversation context across restarts.

---

## Installation

```bash
cd job_agent
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium      # for JS-rendered offer pages
```

### Environment variables

```bash
cp .env.example .env
# Edit .env with your values
source .env
```

Required:
- `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME`,
  `AZURE_OPENAI_API_VERSION` — from Azure Portal (your Azure OpenAI resource)
- `TELEGRAM_BOT_TOKEN` — from @BotFather on Telegram
- `TELEGRAM_CHAT_ID` — your personal chat ID (send /start to @userinfobot)

---

## Configuration

### `config/settings.yaml`

```yaml
azure_openai:
  api_key: "${AZURE_OPENAI_API_KEY}"
  endpoint: "${AZURE_OPENAI_ENDPOINT}"
  deployment: "${AZURE_OPENAI_DEPLOYMENT_NAME}"   # e.g. gpt-4o-mini
  api_version: "${AZURE_OPENAI_API_VERSION}"      # e.g. 2025-01-01-preview

# Single note (legacy) — used by `respond`/`cover-letter`
obsidian_profile_path: "/home/sergio/obsidian/Profile.md"

# Full vault scan — used by the graph's load_context node
obsidian_vault_path: "/home/sergio/obsidian/vault"
obsidian:
  cache_ttl_seconds: 3600   # re-scan at most once per hour

graph:
  checkpoint_db: "data/graph_memory.sqlite"  # persistent cross-session memory

fetcher:
  prefer_playwright: true   # JS-rendered pages; falls back to requests

scheduler:
  search_time: "09:00"
  timezone: "Europe/Madrid"
```

### `config/profile.yaml`

Your full profile. Edit freely — no code changes needed.

### `config/keywords.yaml`

Search keywords, location filters, salary floor, exclusion list.

---

## Usage

### ⭐ Analyze an offer by URL (LangGraph agent)

The headline command. Runs the full graph autonomously: fetch → analyze → extract
questionnaire → answer → recommend.

```bash
# Full analysis (fit score, tech gaps, objections, questionnaire answers, recommendation)
python agent.py analyze --url "https://www.linkedin.com/jobs/view/123456789"

# Dry-run — run the graph but skip notify/persist
python agent.py analyze --url "https://..." --dry-run

# Save a markdown report
python agent.py analyze --url "https://..." --output report.md

# Raw JSON state (for piping/automation)
python agent.py analyze --url "https://..." --json-output
```

Output includes: fit score 1-10, matched vs missing tech, strengths, weaknesses,
likely recruiter objections + rebuttals, auto-answered questionnaire (if present),
apply/skip recommendation, CV tips, and a cover letter.

### Module 1 — Questionnaire Responses

```bash
# Inline text
python agent.py respond \
  --offer "Buscamos Data Engineer con experiencia en Azure Databricks y Python..." \
  --questions "¿Años de experiencia con Python?|¿Tienes certificaciones Azure?|¿Disponibilidad?"

# From files
python agent.py respond --offer offer.txt --questions questions.txt

# Save to file
python agent.py respond --offer offer.txt --questions questions.txt --output responses.md

# Also generate cover letter
python agent.py respond --offer offer.txt --questions questions.txt --cover-letter

# JSON output (for piping)
python agent.py respond --offer offer.txt --questions questions.txt --json-output
```

Questions file format — any of these work:
```
¿Años de experiencia con Python?
¿Tienes certificaciones Azure?
¿Disponibilidad para incorporación?
```
or pipe-separated: `pregunta1|pregunta2|pregunta3`

### Cover Letter

```bash
python agent.py cover-letter --offer offer.txt --output carta.md
```

### Fit Scoring

```bash
python agent.py score --offer offer.txt --title "Data Engineer" --company "Accenture"
```

### Module 2 — Job Search

```bash
# Manual search (saves to DB, scores all jobs)
python agent.py search

# Dry run (print results, no DB writes, no notifications)
python agent.py search --dry-run

# Skip scoring (faster, no LLM API calls)
python agent.py search --no-score

# Single keyword
python agent.py search --keyword "LLM Engineer" --dry-run

# Single source
python agent.py search --source tecnoempleo
```

### Start Bot + Scheduler

```bash
python agent.py serve
python agent.py serve --dry-run   # suppress notifications
```

### Status Dashboard

```bash
python agent.py status
```

---

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/analizar` | **Analyze an offer by URL** — runs the full LangGraph agent |
| `/buscar` | Trigger immediate search |
| `/responder` | Start questionnaire flow (conversational) |
| `/carta` | Generate cover letter (conversational) |
| `/estado` | Show candidature stats |
| `/ofertas` | List latest new jobs |
| `/cancelar` | Cancel ongoing operation |
| `/ayuda` | Help |

`/analizar` accepts the URL inline (`/analizar https://...`) or prompts for it.
Each Telegram user gets their own persistent graph `thread_id`, so the agent keeps
context per conversation. Long results are auto-split into multiple messages.

---

## Obsidian Integration

Two independent mechanisms:

**1. Single profile note** (`obsidian_profile_path`) — used by `respond`/`cover-letter`.
If the `.md` is newer than `config/profile.yaml`, its content becomes the primary context.

**2. Full vault scan** (`obsidian_vault_path`) — used by the graph's `load_context` node
(`modules/memory/obsidian_memory.py`):
- Recursively reads all `.md` notes, **excluding** `.obsidian`, templates, attachments
- Sorts by recency (most recently edited first = highest priority)
- Injects active processes, researched companies, interview notes into the system prompt
- **1-hour TTL cache** — the whole vault isn't re-read on every call
- Notes take priority over the static YAML for the freshest info

Recommended note structure:
```markdown
---
tags: [profile, job-search]
---

# Sergio Rincón — Profile

## Current focus
Looking for Azure Data Engineer / AI Developer roles in Madrid...

## Recent updates
- Completed DP-100 certification (May 2026)
- NoShow Predictor deployed to production
```

---

## Project Structure

```
job_agent/
├── config/
│   ├── profile.yaml         ← your full professional profile
│   ├── keywords.yaml        ← search terms, filters, exclusions
│   └── settings.yaml        ← API keys, models, scheduler, Obsidian, graph
├── modules/
│   ├── graph/               ← LangGraph agent
│   │   ├── agent_graph.py   ← graph assembly + JobAgentGraph wrapper
│   │   ├── nodes.py         ← all node implementations (GraphNodes)
│   │   ├── router.py        ← conditional routing functions
│   │   ├── state.py         ← AgentState TypedDict
│   │   ├── llm.py           ← AzureChatOpenAI wrapper + JSON parsing
│   │   └── formatting.py    ← render state → terminal / Telegram chunks
│   ├── memory/
│   │   ├── obsidian_memory.py ← vault scanner (TTL cache)
│   │   └── persistence.py     ← MemorySaver / SqliteSaver checkpointer
│   ├── scraper/
│   │   ├── url_fetcher.py   ← on-demand Playwright/requests extractor
│   │   ├── base_scraper.py  ← abstract scraper (legacy search)
│   │   ├── infojobs.py      ← Infojobs scraper (legacy search)
│   │   └── tecnoempleo.py   ← Tecnoempleo scraper (legacy search)
│   ├── generator/
│   │   ├── prompts.py       ← all prompt templates (incl. graph nodes)
│   │   └── responder.py     ← AzureChatOpenAI client (respond/score legacy)
│   ├── notifier/
│   │   └── telegram_bot.py  ← bot handlers (incl. /analizar) + notifications
│   ├── storage/
│   │   └── database.py      ← SQLite layer (jobs + responses)
│   └── utils.py             ← config loading, single-note Obsidian
├── data/
│   ├── jobs.db              ← job listings (auto-created)
│   └── graph_memory.sqlite  ← LangGraph checkpoints (auto-created)
├── logs/
│   └── agent.log            ← auto-created on first run
├── agent.py                 ← CLI entry point
├── requirements.txt
└── .env.example
```

---

## Scraper Notes

Both scrapers use `requests` + `BeautifulSoup4`. If a site changes its HTML structure:

1. Run with `--dry-run` to see what's being fetched
2. Inspect the HTML in DevTools and update the CSS selectors in `infojobs.py` or `tecnoempleo.py`
3. If JavaScript rendering is needed, set `use_playwright: true` in `settings.yaml`
   and install: `pip install playwright && playwright install chromium`

LinkedIn bulk scraping is intentionally not supported (respects LinkedIn's Terms of Service).
Individual job offer URLs are analyzed on-demand only when the user explicitly provides them.

---

## Job Status Lifecycle

```
nueva → revisada → aplicada
                → descartada
```

Update manually via SQLite:
```bash
sqlite3 data/jobs.db "UPDATE jobs SET status='aplicada' WHERE id=42"
```

---

## Adding a New Source

1. Create `modules/scraper/mysource.py` extending `BaseScraper`
2. Implement `search()` and `get_details()`
3. Add it to `_run_search()` in `agent.py`
4. Add `mysource: enabled: true` in `config/keywords.yaml`
