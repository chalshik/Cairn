# Cairn — Claude Code Guide

Cairn is an MCP server that scrapes company careers pages and exposes structured job data as MCP tools. Claude Code uses it directly via `mcp__cairn__*` tool calls.

## Architecture

```
main.py      — MCP tool definitions (scrape_jobs, filter_jobs, get_job_detail)
scraper.py   — Platform detection + scraping logic
parser.py    — Normalisation, deduplication, filtering
```

### Platform detection order (scraper.py `scrape_jobs`)

1. Greenhouse → public REST API
2. Lever → public REST API
3. Workday → CX services API (paginated)
4. Ashby → public posting API
5. SmartRecruiters → public API
6. Google Careers → unofficial JSON API
7. Rippling ATS → public API
8. Recruitee → public API
9. Breezy HR → public JSON endpoint
10. Workable → widget API
11. Jobvite → public API
12. BambooHR → static embed HTML
13. Static HTML → httpx + BeautifulSoup heuristics
14. Playwright → headless Chromium (last resort; handles JS SPAs, infinite scroll, hash-fragment routing)

## Return Format

`scrape_jobs` returns a `ScrapeResult` dict, not a bare array:

```json
{
  "platform": "greenhouse",
  "total": 142,
  "jobs": [
    {
      "title": "Senior Engineer",
      "department": "Engineering",
      "location": "Remote – US",
      "url": "https://...",
      "description": "...",
      "posted_date": "2024-03-01"
    }
  ]
}
```

`filter_jobs` accepts both the full `ScrapeResult` object and a bare jobs array.

## Running

### One command (recommended)

```bash
./setup.sh          # build, start, register with Claude Code
./setup.sh 9000     # custom port
./setup.sh stop     # stop
./setup.sh restart  # restart
./setup.sh logs     # follow logs
```

`setup.sh` builds the image, starts the container, waits for the health check to pass, then registers the MCP server with Claude Code automatically.

### Manual Docker

```bash
docker compose up -d --build
claude mcp add --transport sse cairn http://localhost:8000/sse
```

### Local (no Docker)

```bash
pip install mcp httpx beautifulsoup4 playwright
playwright install chromium
claude mcp add cairn -- python main.py
```

## Adding a New Platform

1. Add `_is_<platform>(url) -> str | None` detector in `scraper.py`
2. Add `_scrape_<platform>(slug) -> JobList` scraper
3. Insert detection into the `scrape_jobs()` chain before the static HTML fallback
4. Return via `_make_result("platform_name", jobs)`

## Key Conventions

- All scrapers return `JobList` (list of dicts matching `_empty_job()` schema)
- Use `_retry(fn, *args)` for network calls — 3 attempts with exponential backoff
- HTML descriptions: always strip with `BeautifulSoup(...).get_text(separator="\n").strip()`
- Job detail extraction falls through: Workday API → static httpx → Playwright
- Playwright is async internally; use `_run_async(coro)` to call from sync context

## Dependencies

- `httpx` — HTTP client for API-based platforms
- `beautifulsoup4` — HTML parsing
- `playwright` — headless Chromium for JS-rendered pages
- `mcp` — MCP Python SDK (FastMCP)
