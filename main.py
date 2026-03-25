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
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from scraper import scrape_jobs as _scrape_jobs, scrape_job_detail
from parser import process_jobs, filter_jobs as _filter_jobs, group_by_department

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
# Output formatting
# ---------------------------------------------------------------------------

def _format_jobs(jobs: list[dict[str, Any]], platform: str, total: int, *, keyword: str = "") -> str:
    """
    Format a job list into a grouped, human-readable report.

    Structure:
      Header   — platform, counts, department breakdown
      Sections — one per department, jobs listed with location + URL
      Footer   — quick-filter hint
    """
    count = len(jobs)

    if not jobs:
        msg = f"No jobs found on platform '{platform}'."
        if keyword:
            msg += f" (filter: '{keyword}')"
        if total:
            msg += f" Source reported {total} total before filtering."
        return msg

    groups = group_by_department(jobs)
    dept_summary = ", ".join(
        f"{dept} ({len(js)})" for dept, js in sorted(groups.items())
    )

    # ── Header ──────────────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"  Platform : {platform}")
    lines.append(f"  Jobs     : {count}" + (f" of {total} total" if total > count else f" total"))
    if keyword:
        lines.append(f"  Filter   : '{keyword}'")
    lines.append(f"  Depts    : {len(groups)}")
    lines.append("=" * 60)
    lines.append("")

    # ── Per-department sections ──────────────────────────────────────────────
    for dept in sorted(groups):
        dept_jobs = groups[dept]
        header = f"  {dept}  ({len(dept_jobs)})"
        lines.append(header)
        lines.append("  " + "─" * 56)

        for job in dept_jobs:
            title    = job.get("title", "Untitled")
            location = job.get("location", "")
            url      = job.get("url", "")
            posted   = (job.get("posted_date") or "")[:10]

            loc_part    = f"  [{location}]" if location else ""
            posted_part = f"  posted {posted}" if posted else ""
            lines.append(f"  • {title}{loc_part}{posted_part}")
            if url:
                lines.append(f"    {url}")

        lines.append("")

    # ── Footer ───────────────────────────────────────────────────────────────
    lines.append("─" * 60)
    if not keyword:
        lines.append("  Tip: pass this output to filter_jobs to narrow by keyword.")
    else:
        lines.append(f"  Tip: use get_job_detail(url) to fetch a full description.")
    lines.append("─" * 60)

    return "\n".join(lines)


def _format_job_detail(url: str, description: str) -> str:
    """Format a single job description into a clean, sectioned report."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("  JOB DETAIL")
    lines.append("=" * 60)
    lines.append(f"  URL: {url}")
    lines.append("")

    # Try to detect and label common sections in the description
    section_markers = [
        "responsibilities", "requirements", "qualifications",
        "what you'll do", "what you will do", "about the role",
        "about this role", "who you are", "what you bring",
        "minimum qualifications", "preferred qualifications",
        "basic qualifications", "nice to have", "benefits",
        "about us", "about the team",
    ]

    raw_lines = description.splitlines()
    in_section = False
    for raw in raw_lines:
        stripped = raw.strip()
        low = stripped.lower().rstrip(":").strip()
        if low in section_markers:
            lines.append("")
            lines.append(f"  ── {stripped.upper()} ──")
            in_section = True
        elif stripped:
            prefix = "  " if in_section else "  "
            lines.append(f"{prefix}{stripped}")
        else:
            lines.append("")

    lines.append("")
    lines.append("─" * 60)
    return "\n".join(lines)


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

    Returns a human-readable report grouped by department, followed by a JSON
    block containing the raw job list for downstream use with filter_jobs.

    Each job includes: title, department, location, url, posted_date.

    Args:
        url: The full URL of the careers page.
    """
    result = _scrape_jobs(url)
    jobs   = process_jobs(result["jobs"])
    result["jobs"] = jobs

    report = _format_jobs(jobs, result["platform"], result["total"])

    # Append compact JSON so filter_jobs / get_job_detail can consume it
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return f"{report}\n\n```json\n{payload}\n```"


# ---------------------------------------------------------------------------
# Tool: filter_jobs
# ---------------------------------------------------------------------------

@mcp.tool()
def filter_jobs(jobs: str, keyword: str) -> str:
    """
    Filter a list of job postings by keyword.

    Matches against title, department, location, and description.
    Supports advanced query syntax:
      - Multi-word:      senior engineer       (all words must be present)
      - Exact phrase:    "staff engineer"
      - Field-specific:  title:backend  location:remote  dept:engineering
      - Exclusion:       -manager  -"team lead"

    Accepts the full scrape_jobs output (text + JSON block) or a raw JSON
    object / array.

    Returns a grouped, human-readable report of matching jobs plus a JSON
    block for further chaining.

    Args:
        jobs:    Output from scrape_jobs or a raw JSON jobs payload.
        keyword: Search term(s), e.g. "senior engineer remote" or "title:backend -manager".
    """
    # Extract JSON from the scrape_jobs markdown block if present
    raw = jobs
    if "```json" in jobs:
        start = jobs.index("```json") + 7
        end   = jobs.index("```", start)
        raw   = jobs[start:end].strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"Error: could not parse jobs JSON — {exc}"

    if isinstance(parsed, dict):
        job_list: list[dict[str, Any]] = parsed.get("jobs", [])
        platform = parsed.get("platform", "unknown")
        original_total = parsed.get("total", len(job_list))
    else:
        job_list = parsed
        platform = "unknown"
        original_total = len(job_list)

    results = _filter_jobs(job_list, keyword)

    report = _format_jobs(results, platform, original_total, keyword=keyword)

    payload = json.dumps(
        {"platform": platform, "total": len(results), "jobs": results},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{report}\n\n```json\n{payload}\n```"


# ---------------------------------------------------------------------------
# Tool: get_job_detail
# ---------------------------------------------------------------------------

@mcp.tool()
def get_job_detail(job_url: str) -> str:
    """
    Fetch the full description of a single job posting.

    Retrieves and extracts the complete job description from the posting URL,
    including responsibilities, requirements, qualifications, and any additional
    details. Common sections are automatically labelled for easy scanning.

    Args:
        job_url: The direct URL of a job posting (from the url field in scrape_jobs output).
    """
    try:
        description = scrape_job_detail(job_url)
        return _format_job_detail(job_url, description)
    except Exception as exc:
        return f"Error fetching job detail: {exc}\nURL: {job_url}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("CAIRN_TRANSPORT", "stdio")
    if transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
