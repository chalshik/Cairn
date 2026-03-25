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

_FIELD_ALIASES = {
    "title": "title",
    "t": "title",
    "location": "location",
    "loc": "location",
    "l": "location",
    "department": "department",
    "dept": "department",
    "d": "department",
    "description": "description",
    "desc": "description",
}


def _parse_filter_query(keyword: str) -> list[tuple[str | None, str, bool]]:
    """
    Parse a filter query into a list of (field, term, negated) triples.

    Syntax:
      - plain word       → match anywhere
      - "quoted phrase"  → match exact phrase anywhere
      - field:word       → match in specific field (title, location, dept, description)
      - -word            → exclude jobs containing this word
      - -"phrase"        → exclude jobs containing this phrase

    Examples:
      senior engineer remote        → all three words must appear anywhere
      title:"staff engineer"        → exact phrase in title
      location:remote -manager      → remote location, not a manager role
    """
    tokens: list[tuple[str | None, str, bool]] = []
    # Tokenise: field:"phrase", field:word, "phrase", -"phrase", -word, word
    pattern = re.compile(
        r'(\w+):"([^"]+)"'   # field:"phrase"
        r'|(\w+):(\S+)'       # field:word
        r'|-"([^"]+)"'        # -"phrase"
        r'|-(\S+)'            # -word
        r'|"([^"]+)"'         # "phrase"
        r'|(\S+)'             # word
    )
    for m in pattern.finditer(keyword):
        if m.group(1) and m.group(2):   # field:"phrase"
            field = _FIELD_ALIASES.get(m.group(1).lower())
            tokens.append((field, m.group(2).lower(), False))
        elif m.group(3) and m.group(4): # field:word
            field = _FIELD_ALIASES.get(m.group(3).lower())
            tokens.append((field, m.group(4).lower(), False))
        elif m.group(5):                # -"phrase"
            tokens.append((None, m.group(5).lower(), True))
        elif m.group(6):                # -word
            tokens.append((None, m.group(6).lower(), True))
        elif m.group(7):                # "phrase"
            tokens.append((None, m.group(7).lower(), False))
        elif m.group(8):                # word
            tokens.append((None, m.group(8).lower(), False))
    return tokens


def filter_jobs(jobs: JobList, keyword: str) -> JobList:
    """
    Filter jobs by keyword with advanced query syntax.

    Basic:
      senior engineer          → all words must appear anywhere in the job
    Phrase:
      "staff engineer"         → exact phrase match
    Field-specific:
      title:backend            → word must appear in title
      location:remote          → word must appear in location
      dept:engineering         → word must appear in department
    Exclusion:
      -manager                 → exclude jobs containing "manager"
    Combined:
      title:"data engineer" location:remote -senior
    """
    if not keyword.strip():
        return jobs

    tokens = _parse_filter_query(keyword)
    if not tokens:
        return jobs

    _field_text = {
        "title": lambda j: j.get("title", "").lower(),
        "department": lambda j: j.get("department", "").lower(),
        "location": lambda j: j.get("location", "").lower(),
        "description": lambda j: j.get("description", "").lower(),
    }

    def _haystack(job: dict, field: str | None) -> str:
        if field and field in _field_text:
            return _field_text[field](job)
        return " ".join(
            job.get(f, "") for f in ("title", "department", "location", "description")
        ).lower()

    results: JobList = []
    for job in jobs:
        match = True
        for field, term, negated in tokens:
            hay = _haystack(job, field)
            found = term in hay
            if negated and found:
                match = False
                break
            if not negated and not found:
                match = False
                break
        if match:
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
