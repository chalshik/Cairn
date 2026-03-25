# Cairn

Cairn is an MCP server that turns any company's careers page into structured, queryable job data for AI agents. Point it at a Greenhouse board, a Lever listing, a Workday portal, or a plain HTML careers page — Cairn scrapes all postings without data loss, normalises them into a consistent schema, and exposes them as MCP tools that Claude (or any MCP-compatible agent) can call directly. No manual copy-pasting, no brittle spreadsheets: just clean job intelligence on demand.

---

## Install

### 1. Clone and enter the repo

```bash
git clone https://github.com/chalshik/cairn.git
cd cairn
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 3. Install Python dependencies

```bash
pip install mcp httpx beautifulsoup4 playwright
```

### 4. Install the Playwright browser

```bash
playwright install chromium
```

---

## Register with Claude Code

```bash
claude mcp add cairn -- python main.py
```

Verify it appears:

```bash
claude mcp list
```

---

## MCP Tools

| Tool | Description |
|------|-------------|
| `scrape_jobs(url)` | Scrape all job postings from a careers page. Returns a JSON array. |
| `filter_jobs(jobs, keyword)` | Filter the array by keyword (title / department / location / description). |
| `get_job_detail(job_url)` | Fetch the full description of a single posting by its URL. |

### Output schema (per job)

```json
{
  "title":       "Senior Backend Engineer",
  "department":  "Engineering",
  "location":    "Remote – US",
  "url":         "https://boards.greenhouse.io/stripe/jobs/12345",
  "description": "Full plain-text description...",
  "posted_date": "2024-03-01T00:00:00Z"
}
```

---

## Supported Platforms

| Platform | Method |
|----------|--------|
| **Greenhouse** | Public API (`boards-api.greenhouse.io`) — fast and lossless |
| **Lever** | Public API (`api.lever.co`) |
| **Workday / custom JS boards** | Playwright (headless Chromium) with scroll + pagination |
| **Static HTML pages** | httpx + BeautifulSoup heuristic extraction |

---

## Example Prompts for Claude

```
Show me all open engineering roles at Stripe.
→ Use scrape_jobs("https://boards.greenhouse.io/stripe")

Find remote senior product manager roles at Notion.
→ Use scrape_jobs on Notion's careers page, then filter_jobs with "senior product manager remote"

What does Figma's Staff Designer role require?
→ Use get_job_detail("<job-url from scrape_jobs output>")

Compare backend engineering openings across Vercel and Linear.
→ scrape_jobs for each, then filter_jobs with "backend engineer" on both results

List all jobs in Berlin at Contentful.
→ scrape_jobs("https://www.contentful.com/careers/") then filter_jobs with "berlin"
```

---

## Docker

### Build

```bash
docker build -t cairn .
```

### Register with Claude Code (Docker)

Claude Code communicates over stdio, so pass `-i` to keep stdin open:

```bash
claude mcp add cairn -- docker run -i --rm cairn
```

### Run manually (for testing)

```bash
docker run -i --rm cairn
```

---

## Project Structure

```
cairn/
├── main.py          # MCP server — tool registration and entry point
├── scraper.py       # Careers page scraper (Greenhouse API, Lever API, httpx, Playwright)
├── parser.py        # Normalisation, deduplication, grouping, and filtering
├── Dockerfile
├── requirements.txt
├── .dockerignore
└── README.md
```

---

## Requirements

- Python 3.11+
- `mcp` — MCP Python SDK
- `httpx` — async-capable HTTP client
- `beautifulsoup4` — HTML parsing
- `playwright` — headless browser for JS-rendered pages
