"""
scraper.py — Cairn careers page scraper.

Platform detection order (fastest / most reliable first):
  1.  Greenhouse        — public REST API
  2.  Lever             — public REST API
  3.  Workday           — CX services API with full pagination
  4.  Ashby             — public posting API
  5.  SmartRecruiters   — public API
  6.  Google Careers    — unofficial search API          [NEW]
  7.  Rippling ATS      — public API                    [NEW]
  8.  Recruitee         — public API                    [NEW]
  9.  Breezy HR         — public JSON endpoint           [NEW]
  10. Workable          — public widget API              [NEW]
  11. Jobvite           — public API                    [NEW]
  12. BambooHR          — static embed HTML              [NEW]
  13. Static HTML       — httpx + BeautifulSoup heuristics
  14. Playwright        — headless Chromium, hash-SPA + infinite scroll

Detail page extraction order:
  1. Workday CX API (structured JSON description)
  2. Static HTTP + BS4 (fast path)
  3. Playwright with consent dismissal, scroll, and content-wait
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import time
from typing import Any, TypedDict
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

JobList = list[dict[str, Any]]


class ScrapeResult(TypedDict):
    platform: str
    total: int
    jobs: JobList


def _empty_job() -> dict[str, Any]:
    return {
        "title": "",
        "department": "",
        "location": "",
        "url": "",
        "description": "",
        "posted_date": None,
    }


def _retry(fn, *args, retries: int = 3, backoff: float = 1.0, **kwargs):
    """Call fn(*args, **kwargs) up to `retries` times with exponential backoff."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                sleep = backoff * (2 ** attempt)
                log.warning("Attempt %d failed (%s), retrying in %.1fs", attempt + 1, exc, sleep)
                time.sleep(sleep)
    raise last  # type: ignore[misc]


def _make_result(platform: str, jobs: JobList, total: int | None = None) -> ScrapeResult:
    return ScrapeResult(platform=platform, total=total if total is not None else len(jobs), jobs=jobs)


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

def _is_greenhouse(url: str) -> str | None:
    patterns = [
        r"boards\.greenhouse\.io/([^/?#]+)",
        r"greenhouse\.io/([^/?#]+)",
        r"([^.]+)\.greenhouse\.io",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _scrape_greenhouse(slug: str) -> JobList:
    api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(api)
        resp.raise_for_status()
        data = resp.json()

    jobs: JobList = []
    for item in data.get("jobs", []):
        job = _empty_job()
        job["title"] = item.get("title", "")
        job["url"] = item.get("absolute_url", "")
        job["posted_date"] = item.get("updated_at", None)
        loc = item.get("location", {})
        job["location"] = loc.get("name", "") if isinstance(loc, dict) else str(loc)
        depts = item.get("departments", [])
        if depts:
            job["department"] = depts[0].get("name", "") if isinstance(depts[0], dict) else str(depts[0])
        content = item.get("content", "")
        if content:
            job["description"] = BeautifulSoup(content, "html.parser").get_text(separator="\n").strip()
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------

def _is_lever(url: str) -> str | None:
    m = re.search(r"jobs\.lever\.co/([^/?#]+)", url)
    return m.group(1) if m else None


def _scrape_lever(slug: str) -> JobList:
    api = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(api)
        resp.raise_for_status()
        data = resp.json()

    jobs: JobList = []
    for item in data:
        job = _empty_job()
        job["title"] = item.get("text", "")
        job["url"] = item.get("hostedUrl", "")
        job["department"] = item.get("categories", {}).get("team", "")
        job["location"] = item.get("categories", {}).get("location", "")
        job["posted_date"] = str(item.get("createdAt", "")) or None
        job["description"] = item.get("descriptionPlain", "").strip()
        if not job["description"]:
            raw = item.get("description", "")
            job["description"] = BeautifulSoup(raw, "html.parser").get_text(separator="\n").strip()
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# Workday
# ---------------------------------------------------------------------------

def _is_workday(url: str) -> tuple[str, str, str] | None:
    """Return (tenant, base_url, job_path) if Workday, else None."""
    m = re.search(
        r"(https?://[^/]*myworkdayjobs\.com)(?:/[a-zA-Z_-]+)?/([^/?#]+)",
        url,
    )
    if not m:
        return None
    base = m.group(1)
    job_path = m.group(2)
    t = re.search(r"//([^.]+)\.", base)
    tenant = t.group(1) if t else ""
    return tenant, base, job_path


def _scrape_workday(tenant: str, base: str, job_path: str) -> JobList:
    """Scrape via Workday CX Services API with full pagination."""
    api = f"{base}/wday/cxs/{tenant}/{job_path}/jobs"
    headers = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    limit = 20
    jobs: JobList = []
    offset = 0
    total: int | None = None

    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        while True:
            payload = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": "",
            }
            resp = client.post(api, json=payload)
            resp.raise_for_status()
            data = resp.json()

            if total is None:
                total = data.get("total", 0)
                log.debug("Workday %s/%s: %d total jobs", tenant, job_path, total)

            postings = data.get("jobPostings", [])
            if not postings:
                break

            for item in postings:
                job = _empty_job()
                job["title"] = item.get("title", "")
                ext = item.get("externalPath", "")
                if ext:
                    job["url"] = f"{base}/en-US/{job_path}{ext}"
                job["location"] = item.get("locationsText", "")
                job["posted_date"] = item.get("postedOn", None)
                for field in item.get("bulletFields", []):
                    if isinstance(field, str) and not job["department"]:
                        job["department"] = field
                jobs.append(job)

            offset += len(postings)
            if total is not None and offset >= total:
                break
            if len(postings) < limit:
                break

    return jobs


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------

def _is_ashby(url: str) -> str | None:
    m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)", url)
    return m.group(1) if m else None


def _scrape_ashby(slug: str) -> JobList:
    api = "https://api.ashbyhq.com/posting-public/apiKey/getAll"
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(api, params={"organizationHostedJobsPageName": slug})
        resp.raise_for_status()
        data = resp.json()

    jobs: JobList = []
    for item in data.get("results", []):
        if not item.get("isListed", True):
            continue
        job = _empty_job()
        job["title"] = item.get("title", "")
        job["url"] = item.get("jobUrl", "")
        job["department"] = item.get("department", "")
        job["location"] = item.get("locationName", "")
        job["posted_date"] = item.get("publishedDate", None)
        desc_html = item.get("descriptionHtml", "")
        if desc_html:
            job["description"] = BeautifulSoup(desc_html, "html.parser").get_text(separator="\n").strip()
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# SmartRecruiters
# ---------------------------------------------------------------------------

def _is_smartrecruiters(url: str) -> str | None:
    m = re.search(r"careers\.smartrecruiters\.com/([^/?#]+)", url)
    return m.group(1) if m else None


def _scrape_smartrecruiters(company_id: str) -> JobList:
    api = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    jobs: JobList = []
    offset = 0
    limit = 100

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        while True:
            resp = client.get(api, params={"limit": limit, "offset": offset, "status": "PUBLIC"})
            resp.raise_for_status()
            data = resp.json()
            items = data.get("content", [])
            if not items:
                break
            for item in items:
                job = _empty_job()
                job["title"] = item.get("name", "")
                job["url"] = item.get("ref", "")
                dept = item.get("department") or {}
                job["department"] = dept.get("label", "") if isinstance(dept, dict) else ""
                loc = item.get("location") or {}
                city = loc.get("city", "")
                country = loc.get("country", "")
                job["location"] = ", ".join(p for p in [city, country] if p)
                job["posted_date"] = item.get("releasedDate", None)
                jobs.append(job)
            offset += len(items)
            if len(items) < limit:
                break

    return jobs


# ---------------------------------------------------------------------------
# Google Careers (NEW)
# ---------------------------------------------------------------------------

def _is_google_careers(url: str) -> bool:
    return bool(
        re.search(r"careers\.google\.com", url)
        or re.search(r"google\.com/about/careers", url)
    )


def _scrape_google_careers(url: str) -> tuple[JobList, int]:
    """Scrape via Google's unofficial jobs search API with full pagination."""
    api = "https://careers.google.com/api/jobs/jobs-v1/search/"
    jobs: JobList = []
    page = 1
    total = 0

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        while True:
            params = {
                "q": "",
                "num": 100,
                "page": page,
                "jlo": "en_US",
                "sort_by": "date",
            }
            try:
                resp = client.get(api, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.warning("Google Careers API error on page %d: %s", page, exc)
                break

            if page == 1:
                total = data.get("count", 0)
                log.debug("Google Careers: %d total jobs", total)

            items = data.get("jobs", [])
            if not items:
                break

            for item in items:
                job = _empty_job()
                job["title"] = item.get("title", "")
                job["url"] = item.get("apply_url", "")
                locs = item.get("locations", [])
                if isinstance(locs, list):
                    job["location"] = "; ".join(str(l) for l in locs)
                cats = item.get("category", [])
                if isinstance(cats, list):
                    job["department"] = "; ".join(str(c) for c in cats)
                elif isinstance(cats, str):
                    job["department"] = cats
                parts = []
                for field in ("description", "qualifications", "responsibilities"):
                    val = item.get(field, "")
                    if val:
                        parts.append(val)
                job["description"] = "\n\n".join(parts)
                job["posted_date"] = item.get("publish_date", None)
                jobs.append(job)

            if not data.get("next_page"):
                break
            page += 1

    return jobs, total


# ---------------------------------------------------------------------------
# Rippling ATS (NEW)
# ---------------------------------------------------------------------------

def _is_rippling(url: str) -> str | None:
    m = re.search(r"riptide\.rippling-ats\.com/([^/?#]+)", url)
    return m.group(1) if m else None


def _scrape_rippling(company_slug: str) -> JobList:
    api = "https://riptide.rippling-ats.com/api/ats/jobs/"
    jobs: JobList = []

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(api, params={"company_slug": company_slug, "status": "ACTIVE"})
        resp.raise_for_status()
        data = resp.json()

    items = data if isinstance(data, list) else data.get("results", [])
    for item in items:
        job = _empty_job()
        job["title"] = item.get("title", "") or item.get("name", "")
        job["department"] = item.get("department", "") or item.get("team", "")
        loc = item.get("location", {})
        if isinstance(loc, dict):
            parts = [loc.get("city", ""), loc.get("state", ""), loc.get("country", "")]
            job["location"] = ", ".join(p for p in parts if p)
        elif isinstance(loc, str):
            job["location"] = loc
        job_id = item.get("id", "")
        job["url"] = (
            f"https://riptide.rippling-ats.com/{company_slug}/position/{job_id}"
            if job_id else ""
        )
        job["posted_date"] = item.get("created_at", None)
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# Recruitee (NEW)
# ---------------------------------------------------------------------------

def _is_recruitee(url: str) -> str | None:
    m = re.search(r"([^./]+)\.recruitee\.com", url)
    return m.group(1) if m else None


def _scrape_recruitee(slug: str) -> JobList:
    api = f"https://api.recruitee.com/c/{slug}/positions"
    jobs: JobList = []

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(api)
        resp.raise_for_status()
        data = resp.json()

    for item in data.get("offers", []):
        job = _empty_job()
        job["title"] = item.get("title", "")
        job_slug = item.get("slug", "")
        job["url"] = f"https://{slug}.recruitee.com/o/{job_slug}/" if job_slug else ""
        job["department"] = item.get("department", "")
        city = item.get("city", "")
        country = item.get("country", "")
        job["location"] = ", ".join(p for p in [city, country] if p)
        job["posted_date"] = item.get("created_at", None)
        desc_html = item.get("description", "")
        if desc_html:
            job["description"] = BeautifulSoup(desc_html, "html.parser").get_text(separator="\n").strip()
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# Breezy HR (NEW)
# ---------------------------------------------------------------------------

def _is_breezy(url: str) -> str | None:
    m = re.search(r"([^./]+)\.breezy\.hr", url)
    return m.group(1) if m else None


def _scrape_breezy(company: str) -> JobList:
    api = f"https://{company}.breezy.hr/json"
    jobs: JobList = []

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(api)
        resp.raise_for_status()
        data = resp.json()

    items = data if isinstance(data, list) else data.get("positions", [])
    for item in items:
        if item.get("state", "published") != "published":
            continue
        job = _empty_job()
        job["title"] = item.get("name", "")
        friendly = item.get("friendly_id", item.get("_id", ""))
        job["url"] = f"https://{company}.breezy.hr/p/{friendly}" if friendly else ""
        dept = item.get("department", {})
        job["department"] = dept.get("name", "") if isinstance(dept, dict) else str(dept or "")
        loc = item.get("location", {})
        if isinstance(loc, dict):
            job["location"] = loc.get("name", "")
        elif isinstance(loc, str):
            job["location"] = loc
        job["posted_date"] = item.get("creation_date", None)
        desc_html = item.get("description", "")
        if desc_html:
            job["description"] = BeautifulSoup(desc_html, "html.parser").get_text(separator="\n").strip()
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# Workable (NEW)
# ---------------------------------------------------------------------------

def _is_workable(url: str) -> str | None:
    m = re.search(r"apply\.workable\.com/([^/?#]+)", url)
    return m.group(1) if m else None


def _scrape_workable(company: str) -> JobList:
    api = f"https://apply.workable.com/api/v1/widget/accounts/{company}/jobs"
    jobs: JobList = []

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(api)
        resp.raise_for_status()
        data = resp.json()

    for item in data.get("results", []):
        job = _empty_job()
        job["title"] = item.get("title", "")
        shortcode = item.get("shortcode", "")
        job["url"] = f"https://apply.workable.com/{company}/j/{shortcode}/" if shortcode else ""
        job["department"] = item.get("department", "")
        loc = item.get("location", {})
        if isinstance(loc, dict):
            parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
            job["location"] = ", ".join(p for p in parts if p)
        job["posted_date"] = item.get("published_on", None)
        desc_html = item.get("description", "")
        if desc_html:
            job["description"] = BeautifulSoup(desc_html, "html.parser").get_text(separator="\n").strip()
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# Jobvite (NEW)
# ---------------------------------------------------------------------------

def _is_jobvite(url: str) -> str | None:
    m = re.search(r"jobs\.jobvite\.com/([^/?#]+)", url)
    return m.group(1) if m else None


def _scrape_jobvite(company_id: str) -> JobList:
    api = f"https://api.jobvite.com/api/v2/{company_id}/position"
    jobs: JobList = []

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(api, params={"jobStatus": "Open", "start": 0, "count": 500})
        resp.raise_for_status()
        data = resp.json()

    for item in data.get("position", []):
        job = _empty_job()
        job["title"] = item.get("title", "")
        job["url"] = item.get("jobApplyUrl", "") or item.get("jobUrl", "")
        job["department"] = item.get("category", "")
        loc = item.get("location", "")
        if isinstance(loc, str):
            job["location"] = loc
        elif isinstance(loc, dict):
            city = loc.get("city", "")
            state = loc.get("state", "")
            job["location"] = ", ".join(p for p in [city, state] if p)
        job["posted_date"] = item.get("date", None)
        desc_html = item.get("description", "")
        if desc_html:
            job["description"] = BeautifulSoup(desc_html, "html.parser").get_text(separator="\n").strip()
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# BambooHR (NEW)
# ---------------------------------------------------------------------------

def _is_bamboohr(url: str) -> str | None:
    m = re.search(r"([^./]+)\.bamboohr\.com", url)
    return m.group(1) if m else None


def _scrape_bamboohr(company: str) -> JobList:
    """Scrape BambooHR static embed widget — no JS rendering needed."""
    embed_url = f"https://{company}.bamboohr.com/jobs/embed2/"
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(embed_url)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    jobs: JobList = []
    seen: set[str] = set()

    # BambooHR embed: <li class="jss-jobs-list__item"> or <div class="BambooHR-ATS-Jobs-Item">
    for li in soup.select(
        "li[class*='jobs-list'], li[class*='jss-jobs-list'], "
        "div[class*='BambooHR-ATS-Jobs-Item'], div[class*='jobs-listing-item']"
    ):
        job = _empty_job()
        a = li.find("a", href=True)
        if a:
            job["title"] = a.get_text(strip=True)
            href = a.get("href", "")
            job["url"] = urljoin(f"https://{company}.bamboohr.com", href)
        for span in li.find_all(["span", "div", "li"]):
            text = span.get_text(strip=True)
            low = text.lower()
            if not job["location"] and any(kw in low for kw in _LOCATION_KEYWORDS):
                job["location"] = text
            elif not job["department"] and any(kw in low for kw in _DEPT_KEYWORDS):
                job["department"] = text
        if job["title"] and job["url"] not in seen:
            seen.add(job["url"])
            jobs.append(job)

    # Fallback: any anchor pointing to /jobs/{id}/view
    if not jobs:
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/jobs/" in href:
                title = a.get_text(strip=True)
                full_url = urljoin(f"https://{company}.bamboohr.com", href)
                if title and full_url not in seen:
                    job = _empty_job()
                    job["title"] = title
                    job["url"] = full_url
                    seen.add(full_url)
                    jobs.append(job)

    return jobs


# ---------------------------------------------------------------------------
# Static scraper (httpx + BeautifulSoup)
# ---------------------------------------------------------------------------

_JOB_PATH_KEYWORDS = (
    "/job", "/career", "/position", "/opening", "/role",
    "/posting", "/vacancy", "/apply", "/jobs/",
)

_JOB_SELECTORS = [
    "a[href*='/job']",
    "a[href*='/career']",
    "a[href*='/position']",
    "a[href*='/opening']",
    "a[href*='/role']",
    "a[href*='/posting']",
    "a[href*='/vacancy']",
    "li.job",
    "div.job",
    "tr.job",
    "[data-job-id]",
    "[data-job]",
    "[class*='job-listing']",
    "[class*='job-item']",
    "[class*='job-card']",
    "[class*='career-listing']",
    "[class*='position-item']",
    "[class*='opening-item']",
    "[class*='role-item']",
]

_LOCATION_KEYWORDS = {
    "remote", "hybrid", "onsite", "on-site", "office",
    "new york", "london", "berlin", "paris", "san francisco",
    "seattle", "austin", "boston", "chicago", "toronto",
    "amsterdam", "singapore", "tokyo", "sydney", "tel aviv",
    "bangalore", "mumbai", "zürich", "zurich", "dublin",
    "stockholm", "copenhagen", "helsinki", "warsaw", "madrid",
    "barcelona", "rome", "milan", "vienna", "prague",
}

_DEPT_KEYWORDS = {
    "engineering", "product", "design", "sales", "marketing",
    "finance", "operations", "people", "legal", "data", "research",
    "security", "infrastructure", "platform", "business", "customer",
    "support", "recruiting", "hr", "growth", "analytics", "science",
    "machine learning", "ai", "hardware", "firmware", "mobile",
}


def _scrape_static(url: str) -> JobList:
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return _parse_html_jobs(resp.text, url)


def _parse_html_jobs(html: str, base_url: str) -> JobList:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select(
        "nav, header, footer, [role='navigation'], "
        "[class*='sidebar'], [class*='nav-'], [class*='menu']"
    ):
        tag.decompose()

    jobs: JobList = []
    seen_urls: set[str] = set()

    for sel in _JOB_SELECTORS:
        for el in soup.select(sel):
            job = _extract_job_from_element(el, base_url)
            if job and job["title"] and job["url"] not in seen_urls:
                seen_urls.add(job["url"])
                jobs.append(job)

    if not jobs:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if not (5 <= len(text) <= 200):
                continue
            full_url = urljoin(base_url, a["href"])
            if full_url in seen_urls:
                continue
            parsed = urlparse(full_url)
            if any(kw in parsed.path.lower() for kw in _JOB_PATH_KEYWORDS):
                job = _empty_job()
                job["title"] = text
                job["url"] = full_url
                seen_urls.add(full_url)
                jobs.append(job)

    return jobs


def _extract_job_from_element(el: Any, base_url: str) -> dict[str, Any] | None:
    job = _empty_job()

    if el.name == "a":
        job["title"] = el.get_text(strip=True)
        href = el.get("href", "")
        job["url"] = urljoin(base_url, href) if href else ""
    else:
        a = el.find("a", href=True)
        if a:
            job["title"] = a.get_text(strip=True) or el.get_text(strip=True)
            job["url"] = urljoin(base_url, a["href"])
        else:
            job["title"] = el.get_text(strip=True)

    for node in list(el.stripped_strings)[1:]:
        low = node.lower()
        if not job["location"] and any(kw in low for kw in _LOCATION_KEYWORDS):
            job["location"] = node
        elif not job["department"] and any(kw in low for kw in _DEPT_KEYWORDS):
            job["department"] = node

    if not job["title"] or len(job["title"]) > 200:
        return None
    return job


# ---------------------------------------------------------------------------
# Playwright scraper (improved: hash-SPA + infinite scroll + click pagination)
# ---------------------------------------------------------------------------

_PAGINATION_SELECTORS = [
    "button:has-text('Load more')",
    "button:has-text('Show more')",
    "button:has-text('View more')",
    "button:has-text('See more')",
    "button:has-text('More jobs')",
    "button:has-text('Load More')",
    "button:has-text('Show More')",
    "[aria-label='Next page']",
    "[aria-label='Next']",
    "[data-automation='pagination-next']",
    "a:has-text('Next')",
    "a[rel='next']",
    "nav[aria-label*='agination'] a:last-child",
    ".pagination .next a",
    ".pagination-next a",
    ".pager-next a",
    "[class*='pagination'] [class*='next']",
]

_CONSENT_SELECTORS = [
    "button:has-text('Accept all')",
    "button:has-text('Accept cookies')",
    "button:has-text('Accept')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
    "button:has-text('Got it')",
    "[aria-label='Accept cookies']",
    "#onetrust-accept-btn-handler",
    ".cc-accept",
    ".cookie-consent-accept",
]


async def _scrape_playwright_async(url: str) -> JobList:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "playwright is not installed. Run: pip install playwright && playwright install chromium"
        )

    # Hash-fragment SPA: split URL so we can navigate base then push the route
    hash_fragment: str | None = None
    base_url = url
    if "#" in url:
        base_url, frag = url.split("#", 1)
        hash_fragment = frag
        log.debug("Hash-SPA detected: base=%s fragment=#%s", base_url, frag[:60])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            extra_http_headers=HEADERS,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Navigate base URL first
        await page.goto(base_url, wait_until="networkidle", timeout=60_000)

        # For hash-SPAs: push the client-side route and wait for re-render
        if hash_fragment:
            await page.evaluate(f"window.location.hash = {hash_fragment!r}")
            await asyncio.sleep(2.0)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

        # Dismiss cookie/consent banners
        for sel in _CONSENT_SELECTORS:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(timeout=3_000)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

        # Infinite scroll: scroll until page height stabilises 3 times in a row
        last_height: int = await page.evaluate("document.body.scrollHeight")
        stall_count = 0
        while stall_count < 3:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.2)
            new_height: int = await page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                stall_count += 1
            else:
                stall_count = 0
                last_height = new_height
        await page.evaluate("window.scrollTo(0, 0)")

        # Click-based pagination: up to 50 pages
        for _page_num in range(50):
            html_before = await page.content()
            jobs_before = len(_parse_html_jobs(html_before, url))

            clicked = False
            for sel in _PAGINATION_SELECTORS:
                try:
                    btn = page.locator(sel)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.scroll_into_view_if_needed()
                        await btn.first.click(timeout=5_000)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10_000)
                        except Exception:
                            await asyncio.sleep(1.5)
                        for _ in range(4):
                            await page.evaluate("window.scrollBy(0, window.innerHeight)")
                            await asyncio.sleep(0.3)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                break

            html_after = await page.content()
            jobs_after = len(_parse_html_jobs(html_after, url))
            if jobs_after <= jobs_before:
                log.debug("Playwright pagination: no new jobs after click, stopping")
                break

            log.debug("Playwright pagination page %d: %d jobs so far", _page_num + 1, jobs_after)

        html = await page.content()
        await browser.close()

    return _parse_html_jobs(html, url)


def _run_async(coro):
    """Run an async coroutine safely regardless of whether an event loop is running."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


def _scrape_playwright(url: str) -> JobList:
    return _run_async(_scrape_playwright_async(url))


# ---------------------------------------------------------------------------
# Single job detail
# ---------------------------------------------------------------------------

_DETAIL_NOISE_SELECTORS = (
    "nav, header, footer, script, style, noscript, "
    "[role='navigation'], [role='banner'], [role='contentinfo'], "
    "[class*='cookie'], [class*='consent'], [class*='banner'], "
    "[class*='sidebar'], [class*='related'], [class*='recommended'], "
    "[class*='similar-job'], [class*='share'], [class*='social'], "
    "[class*='breadcrumb'], [id*='cookie'], [id*='consent']"
)

_DETAIL_CONTENT_SELECTORS = [
    # Semantic / explicit
    "[class*='job-description']",
    "[id*='job-description']",
    "[class*='job-details']",
    "[id*='job-details']",
    "[class*='job-content']",
    "[id*='job-content']",
    "[class*='posting-details']",
    "[class*='posting-content']",
    "[class*='job-posting']",
    "[class*='career-detail']",
    "[class*='position-detail']",
    "[class*='role-description']",
    # Test IDs used by React/Next apps
    "[data-testid*='job']",
    "[data-testid*='description']",
    "[data-automation*='job']",
    # Generic containers
    "article",
    "[role='main']",
    "main",
    ".content",
    "#content",
    "#main",
    "#main-content",
    "[class*='description']",
    "[id*='description']",
]

_MIN_DESCRIPTION_CHARS = 300

_JOB_CONTENT_SIGNALS = {
    "responsibilities", "requirements", "qualifications", "experience",
    "skills", "you will", "you'll", "what you'll", "what you will",
    "we are looking", "we're looking", "role overview", "about the role",
    "about this role", "job description", "what you bring", "who you are",
    "minimum qualifications", "preferred qualifications", "basic qualifications",
    "must have", "nice to have", "duties", "job summary", "position summary",
}


def _is_content_sufficient(text: str) -> bool:
    if len(text.strip()) < _MIN_DESCRIPTION_CHARS:
        return False
    low = text.lower()
    return any(signal in low for signal in _JOB_CONTENT_SIGNALS)


def _extract_description_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select(_DETAIL_NOISE_SELECTORS):
        tag.decompose()

    for sel in _DETAIL_CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n").strip()
            if _is_content_sufficient(text):
                return text

    return soup.get_text(separator="\n").strip()


def _scrape_workday_detail(job_url: str) -> str | None:
    """Try Workday CX API for a job detail. Returns text or None."""
    m = re.search(
        r"(https?://[^/]*myworkdayjobs\.com)/(?:[a-zA-Z_-]+/)?([^/?#]+)/job/[^/]+/([^/?#]+)",
        job_url,
    )
    if not m:
        return None
    base, job_path, job_id = m.group(1), m.group(2), m.group(3)
    t = re.search(r"//([^.]+)\.", base)
    if not t:
        return None
    tenant = t.group(1)
    api = f"{base}/wday/cxs/{tenant}/{job_path}/jobs/{job_id}"
    try:
        with httpx.Client(headers={**HEADERS, "Accept": "application/json"}, timeout=30, follow_redirects=True) as client:
            resp = client.get(api)
            resp.raise_for_status()
            data = resp.json()
        desc_html = (
            data.get("jobPostingInfo", {}).get("jobDescription", "")
            or data.get("jobDescription", "")
        )
        if desc_html:
            return BeautifulSoup(desc_html, "html.parser").get_text(separator="\n").strip()
    except Exception as exc:
        log.debug("Workday detail API failed for %s: %s", job_url, exc)
    return None


def scrape_job_detail(job_url: str) -> str:
    """Fetch and extract the full description of a single job posting."""
    # 1. Try Workday API (structured, no JS needed)
    wd_desc = _scrape_workday_detail(job_url)
    if wd_desc and _is_content_sufficient(wd_desc):
        return wd_desc

    # 2. Try fast static fetch
    html: str | None = None
    try:
        with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            resp = client.get(job_url)
            if resp.status_code not in (403, 429):
                resp.raise_for_status()
                html = resp.text
    except Exception:
        pass

    if html:
        text = _extract_description_from_html(html)
        if _is_content_sufficient(text):
            return text
        log.debug("Static detail insufficient (%d chars), falling back to Playwright", len(text.strip()))

    # 3. Playwright with full JS rendering
    html = _get_html_playwright_detail(job_url)
    return _extract_description_from_html(html)


async def _get_html_playwright_detail_async(url: str) -> str:
    """Fetch a JS-rendered detail page with consent dismissal, scroll, and content-wait."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "playwright is not installed. Run: pip install playwright && playwright install chromium"
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            extra_http_headers=HEADERS,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        for sel in _CONSENT_SELECTORS:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(timeout=3_000)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

        for _ in range(6):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.3)
        await page.evaluate("window.scrollTo(0, 0)")

        for sel in _DETAIL_CONTENT_SELECTORS[:8]:
            try:
                await page.wait_for_selector(sel, timeout=4_000)
                break
            except Exception:
                continue

        await asyncio.sleep(0.8)

        html = await page.content()
        await browser.close()
    return html


def _get_html_playwright_detail(url: str) -> str:
    return _run_async(_get_html_playwright_detail_async(url))


def _get_html_playwright(url: str) -> str:
    """Legacy alias — kept for backwards compatibility."""
    return _get_html_playwright_detail(url)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_jobs(url: str) -> ScrapeResult:
    """
    Scrape all job postings from a careers page URL.

    Returns a ScrapeResult dict with:
      - platform: detected ATS name
      - total:    total job count reported by the source
      - jobs:     list of job dicts

    Detection order:
      1.  Greenhouse
      2.  Lever
      3.  Workday
      4.  Ashby
      5.  SmartRecruiters
      6.  Google Careers
      7.  Rippling ATS
      8.  Recruitee
      9.  Breezy HR
      10. Workable
      11. Jobvite
      12. BambooHR
      13. Static HTML
      14. Playwright (hash-SPA + infinite scroll)
    """
    slug = _is_greenhouse(url)
    if slug:
        log.debug("Detected Greenhouse: %s", slug)
        return _make_result("greenhouse", _retry(_scrape_greenhouse, slug))

    slug = _is_lever(url)
    if slug:
        log.debug("Detected Lever: %s", slug)
        return _make_result("lever", _retry(_scrape_lever, slug))

    wd = _is_workday(url)
    if wd:
        tenant, base, job_path = wd
        log.debug("Detected Workday: tenant=%s path=%s", tenant, job_path)
        return _make_result("workday", _retry(_scrape_workday, tenant, base, job_path))

    slug = _is_ashby(url)
    if slug:
        log.debug("Detected Ashby: %s", slug)
        return _make_result("ashby", _retry(_scrape_ashby, slug))

    slug = _is_smartrecruiters(url)
    if slug:
        log.debug("Detected SmartRecruiters: %s", slug)
        return _make_result("smartrecruiters", _retry(_scrape_smartrecruiters, slug))

    if _is_google_careers(url):
        log.debug("Detected Google Careers")
        jobs, total = _retry(_scrape_google_careers, url)
        return _make_result("google_careers", jobs, total)

    slug = _is_rippling(url)
    if slug:
        log.debug("Detected Rippling ATS: %s", slug)
        return _make_result("rippling", _retry(_scrape_rippling, slug))

    slug = _is_recruitee(url)
    if slug:
        log.debug("Detected Recruitee: %s", slug)
        return _make_result("recruitee", _retry(_scrape_recruitee, slug))

    slug = _is_breezy(url)
    if slug:
        log.debug("Detected Breezy HR: %s", slug)
        return _make_result("breezy", _retry(_scrape_breezy, slug))

    slug = _is_workable(url)
    if slug:
        log.debug("Detected Workable: %s", slug)
        return _make_result("workable", _retry(_scrape_workable, slug))

    slug = _is_jobvite(url)
    if slug:
        log.debug("Detected Jobvite: %s", slug)
        return _make_result("jobvite", _retry(_scrape_jobvite, slug))

    slug = _is_bamboohr(url)
    if slug:
        log.debug("Detected BambooHR: %s", slug)
        return _make_result("bamboohr", _retry(_scrape_bamboohr, slug))

    # Generic: try static first, fall back to Playwright
    try:
        jobs = _scrape_static(url)
        log.debug("Static scrape: %d jobs", len(jobs))
    except Exception as exc:
        log.warning("Static scrape failed (%s), falling back to Playwright", exc)
        jobs = []

    if len(jobs) < 3:
        log.debug("Too few jobs from static (%d), trying Playwright", len(jobs))
        jobs = _scrape_playwright(url)
        return _make_result("playwright", jobs)

    return _make_result("static_html", jobs)
