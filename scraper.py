"""
scraper.py — Cairn careers page scraper.

Platform detection order (fastest / most reliable first):
  1. Greenhouse  — public REST API
  2. Lever       — public REST API
  3. Workday     — CX services API with full pagination
  4. Ashby       — public posting API
  5. SmartRecruiters — public API
  6. Static HTML — httpx + BeautifulSoup heuristics
  7. Playwright  — headless Chromium, full pagination support

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
from typing import Any
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
    # e.g. https://databricks.wd5.myworkdayjobs.com/en-US/careers
    m = re.search(
        r"(https?://[^/]*myworkdayjobs\.com)(?:/[a-zA-Z_-]+)?/([^/?#]+)",
        url,
    )
    if not m:
        return None
    base = m.group(1)  # https://databricks.wd5.myworkdayjobs.com
    job_path = m.group(2)  # careers
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
                # bulletFields sometimes contains department or work type
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
# Static scraper (httpx + BeautifulSoup)
# ---------------------------------------------------------------------------

# Keyword patterns used to identify job-related anchors in fallback mode
_JOB_PATH_KEYWORDS = (
    "/job", "/career", "/position", "/opening", "/role",
    "/posting", "/vacancy", "/apply", "/jobs/",
)

# CSS selectors tried in order before falling back to all anchors
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

    # Strip navigation noise before scanning for jobs
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

    # Fallback: anchors whose path looks like a job posting
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
# Playwright scraper (JS-rendered pages, full pagination)
# ---------------------------------------------------------------------------

# Ordered list of pagination button selectors
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

# Cookie/consent banner dismissal (tried once at page load)
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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            extra_http_headers=HEADERS,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60_000)

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

        # Initial scroll to trigger lazy loading
        for _ in range(8):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.4)

        # Paginate: click next/load-more up to 50 times
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
                        # Scroll new content into view
                        for _ in range(4):
                            await page.evaluate("window.scrollBy(0, window.innerHeight)")
                            await asyncio.sleep(0.3)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                break

            # Stop if no new jobs loaded after pagination click
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

# Minimum character count to consider static extraction successful
_MIN_DESCRIPTION_CHARS = 300

# At least one of these keywords must appear to confirm real job content
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

    # Last resort: full page text
    return soup.get_text(separator="\n").strip()


def _scrape_workday_detail(job_url: str) -> str | None:
    """
    Try to fetch a Workday job description via the CX Services API.
    Returns the description text, or None if not a Workday URL.
    """
    # Match: https://tenant.wd5.myworkdayjobs.com/en-US/path/job/title/id
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
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
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

        # Load page — use domcontentloaded first (faster), then wait for network
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        # Dismiss consent/cookie banners
        for sel in _CONSENT_SELECTORS:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(timeout=3_000)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

        # Scroll to trigger lazy-loaded content
        for _ in range(6):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.3)
        await page.evaluate("window.scrollTo(0, 0)")

        # Wait for job content to appear
        for sel in _DETAIL_CONTENT_SELECTORS[:8]:  # Try the most specific ones
            try:
                await page.wait_for_selector(sel, timeout=4_000)
                break
            except Exception:
                continue

        # Final settle
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

def scrape_jobs(url: str) -> JobList:
    """
    Scrape all job postings from a careers page URL.

    Detection order:
      1. Greenhouse API
      2. Lever API
      3. Workday CX API
      4. Ashby API
      5. SmartRecruiters API
      6. Static HTML (httpx + BS4)
      7. Playwright (JS-rendered, full pagination)
    """
    slug = _is_greenhouse(url)
    if slug:
        log.debug("Detected Greenhouse: %s", slug)
        return _retry(_scrape_greenhouse, slug)

    slug = _is_lever(url)
    if slug:
        log.debug("Detected Lever: %s", slug)
        return _retry(_scrape_lever, slug)

    wd = _is_workday(url)
    if wd:
        tenant, base, job_path = wd
        log.debug("Detected Workday: tenant=%s path=%s", tenant, job_path)
        return _retry(_scrape_workday, tenant, base, job_path)

    slug = _is_ashby(url)
    if slug:
        log.debug("Detected Ashby: %s", slug)
        return _retry(_scrape_ashby, slug)

    slug = _is_smartrecruiters(url)
    if slug:
        log.debug("Detected SmartRecruiters: %s", slug)
        return _retry(_scrape_smartrecruiters, slug)

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

    return jobs
