"""
parser.py — Job data structuring, deduplication, and filtering for Cairn.
"""

from __future__ import annotations

import re
from typing import Any

JobList = list[dict[str, Any]]

# ---------------------------------------------------------------------------
# Schema enforcement
# ---------------------------------------------------------------------------

_SCHEMA_KEYS = ("title", "department", "location", "url", "description", "posted_date")


def normalize_job(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure a job dict conforms to the output schema."""
    return {
        "title": _clean(raw.get("title", "")),
        "department": _clean(raw.get("department", "")),
        "location": _clean(raw.get("location", "")),
        "url": (raw.get("url") or "").strip(),
        "description": _clean(raw.get("description", "")),
        "posted_date": raw.get("posted_date") or None,
    }


def _clean(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    # Collapse whitespace / newlines
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(jobs: JobList) -> JobList:
    """Remove duplicate postings by URL, falling back to title+location."""
    seen_urls: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    out: JobList = []
    for job in jobs:
        url = job.get("url", "").rstrip("/").lower()
        key = (_clean(job.get("title", "")).lower(), _clean(job.get("location", "")).lower())
        if url and url in seen_urls:
            continue
        if not url and key in seen_keys:
            continue
        if url:
            seen_urls.add(url)
        seen_keys.add(key)
        out.append(job)
    return out


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def group_by_department(jobs: JobList) -> dict[str, JobList]:
    """Return jobs grouped by department name."""
    groups: dict[str, JobList] = {}
    for job in jobs:
        dept = job.get("department") or "Other"
        groups.setdefault(dept, []).append(job)
    # Sort within each group by title
    for dept in groups:
        groups[dept].sort(key=lambda j: j.get("title", "").lower())
    return groups


def group_by_location(jobs: JobList) -> dict[str, JobList]:
    """Return jobs grouped by location."""
    groups: dict[str, JobList] = {}
    for job in jobs:
        loc = job.get("location") or "Unknown"
        groups.setdefault(loc, []).append(job)
    for loc in groups:
        groups[loc].sort(key=lambda j: j.get("title", "").lower())
    return groups


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_jobs(jobs: JobList, keyword: str) -> JobList:
    """
    Filter jobs by keyword.

    Matches against title, department, location, and description (case-insensitive).
    Supports multi-word queries — all words must match somewhere in the job.
    """
    if not keyword.strip():
        return jobs

    terms = keyword.lower().split()

    results: JobList = []
    for job in jobs:
        haystack = " ".join([
            job.get("title", ""),
            job.get("department", ""),
            job.get("location", ""),
            job.get("description", ""),
        ]).lower()
        if all(term in haystack for term in terms):
            results.append(job)
    return results


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def process_jobs(raw_jobs: JobList) -> JobList:
    """Normalize, deduplicate, and sort a raw job list."""
    jobs = [normalize_job(j) for j in raw_jobs]
    jobs = deduplicate(jobs)
    jobs.sort(key=lambda j: (j.get("department", "").lower(), j.get("title", "").lower()))
    return jobs
