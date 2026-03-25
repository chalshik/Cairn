"""
main.py — Cairn MCP server entry point.

Exposes three MCP tools:
  • scrape_jobs(url)            — scrape all job postings from a careers page
  • filter_jobs(jobs, keyword)  — filter a job list by keyword
  • get_job_detail(job_url)     — fetch the full description of a single posting

Transport is selected via the CAIRN_TRANSPORT environment variable:
  stdio  (default) — for local use / Claude Code CLI
  sse              — HTTP+SSE server, used by Docker (default in container)

Run locally:
    python main.py

Run as HTTP server:
    CAIRN_TRANSPORT=sse CAIRN_PORT=8000 python main.py

Register with Claude Code (local):
    claude mcp add cairn -- python main.py

Register with Claude Code (Docker / SSE):
    claude mcp add --transport sse cairn http://localhost:8000/sse
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from scraper import scrape_jobs as _scrape_jobs, scrape_job_detail
from parser import process_jobs, filter_jobs as _filter_jobs

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

_port = int(os.environ.get("CAIRN_PORT", "8000"))
mcp = FastMCP(
    name="cairn",
    instructions=(
        "Cairn scrapes company careers pages and returns structured job postings. "
        "Use scrape_jobs to fetch all jobs from a URL, filter_jobs to narrow results "
        "by keyword, and get_job_detail to fetch the full description of a single posting."
    ),
    host="0.0.0.0",
    port=_port,
)

# ---------------------------------------------------------------------------
# Tool: scrape_jobs
# ---------------------------------------------------------------------------

@mcp.tool()
def scrape_jobs(url: str) -> str:
    """
    Scrape all job postings from a company careers page.

    Supports Greenhouse, Lever, Workday, Ashby, SmartRecruiters, Google Careers,
    Rippling ATS, Recruitee, Breezy HR, Workable, Jobvite, BambooHR, and generic
    HTML / JS-rendered career pages.

    Returns a JSON object with:
      - platform:  detected ATS name (e.g. "greenhouse", "google_careers", "playwright")
      - total:     total job count reported by the source (may exceed jobs list length)
      - jobs:      array of job objects with fields:
                   title, department, location, url, description, posted_date

    Pass the full response object directly to filter_jobs — it accepts both formats.

    Args:
        url: The full URL of the careers page
    """
    result = _scrape_jobs(url)
    result["jobs"] = process_jobs(result["jobs"])
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool: filter_jobs
# ---------------------------------------------------------------------------

@mcp.tool()
def filter_jobs(jobs: str, keyword: str) -> str:
    """
    Filter a list of job postings by keyword.

    Matches against title, department, location, and description.
    Supports advanced query syntax:
      - Multi-word:        "senior engineer"  (all words must be present)
      - Exact phrase:      "staff engineer"   (quoted)
      - Field-specific:    title:backend  location:remote  dept:engineering
      - Exclusion:         -manager  -"team lead"

    Accepts either a raw JSON array of jobs OR the full scrape_jobs output object
    (with platform/total/jobs fields) — both formats work transparently.

    Args:
        jobs:    Output from scrape_jobs (object or array).
        keyword: Search term(s), e.g. "senior engineer remote" or "title:backend -manager".
    """
    try:
        parsed = json.loads(jobs)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"Invalid jobs JSON: {exc}"})

    # Accept both {platform, total, jobs} dict and bare array
    if isinstance(parsed, dict):
        job_list: list[dict[str, Any]] = parsed.get("jobs", [])
        meta = {k: v for k, v in parsed.items() if k != "jobs"}
    else:
        job_list = parsed
        meta = {}

    results = _filter_jobs(job_list, keyword)

    if meta:
        return json.dumps(
            {**meta, "total": len(results), "jobs": results},
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(results, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_job_detail
# ---------------------------------------------------------------------------

@mcp.tool()
def get_job_detail(job_url: str) -> str:
    """
    Fetch the full description of a single job posting.

    Retrieves and extracts the complete job description from the posting URL,
    including responsibilities, requirements, and any additional details.

    Args:
        job_url: The direct URL of a job posting (from the url field of scrape_jobs output).
    """
    try:
        description = scrape_job_detail(job_url)
        return json.dumps({"url": job_url, "description": description}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc), "url": job_url})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("CAIRN_TRANSPORT", "stdio")
    if transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
