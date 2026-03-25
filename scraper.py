"""
scraper.py — Careers page scraper for Cairn.

Strategy (in order):
1. Greenhouse public API (fastest, lossless)
2. Lever public API
3. httpx + BeautifulSoup for static HTML
4. Playwright for JS-rendered pages (Workday, custom React/Vue boards)
"""

from __future__ import annotations

import re
import json
import asyncio
from typing import Any
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

def _is_greenhouse(url: str) -> str | None:
    """Return the board slug if URL points to a Greenhouse board, else None."""
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

        # Location
        loc = item.get("location", {})
        job["location"] = loc.get("name", "") if isinstance(loc, dict) else str(loc)

        # Department — may be list or single
        depts = item.get("departments", [])
        if depts:
            job["department"] = depts[0].get("name", "") if isinstance(depts[0], dict) else str(depts[0])

        # Description from content block
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

        # Description — plain text from lists
        desc_parts: list[str] = []
        for block in item.get("descriptionPlain", "").split("\n"):
            desc_parts.append(block)
        job["description"] = "\n".join(desc_parts).strip()
        if not job["description"]:
            raw = item.get("description", "")
            job["description"] = BeautifulSoup(raw, "html.parser").get_text(separator="\n").strip()

        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# Static scraper (httpx + BeautifulSoup)
# ---------------------------------------------------------------------------

def _scrape_static(url: str) -> JobList:
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text

    return _parse_html_jobs(html, url)


def _parse_html_jobs(html: str, base_url: str) -> JobList:
    soup = BeautifulSoup(html, "html.parser")
    jobs: JobList = []

    # Heuristic: look for repeated anchor-or-li patterns that smell like job rows
    # Common selectors used by career sites
    candidate_selectors = [
        "a[href*='job']",
        "a[href*='career']",
        "a[href*='position']",
        "a[href*='opening']",
        "a[href*='role']",
        "li.job",
        "div.job",
        "tr.job",
        "[data-job-id]",
        "[class*='job-listing']",
        "[class*='career-listing']",
        "[class*='position']",
        "[class*='opening']",
    ]

    seen_urls: set[str] = set()
    for sel in candidate_selectors:
        for el in soup.select(sel):
            job = _extract_job_from_element(el, base_url)
            if job and job["url"] not in seen_urls and job["title"]:
                seen_urls.add(job["url"])
                jobs.append(job)

    # If nothing found, fall back to ALL anchors with job-like text
    if not jobs:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a["href"]
            if len(text) < 5 or len(text) > 200:
                continue
            full_url = urljoin(base_url, href)
            if full_url in seen_urls:
                continue
            job = _empty_job()
            job["title"] = text
            job["url"] = full_url
            seen_urls.add(full_url)
            jobs.append(job)

    return jobs


def _extract_job_from_element(el: Any, base_url: str) -> dict[str, Any] | None:
    job = _empty_job()
    tag = el.name

    if tag == "a":
        job["title"] = el.get_text(strip=True)
        href = el.get("href", "")
        job["url"] = urljoin(base_url, href) if href else ""
    else:
        # Look for a link inside the element
        a = el.find("a", href=True)
        if a:
            job["title"] = a.get_text(strip=True) or el.get_text(strip=True)
            job["url"] = urljoin(base_url, a["href"])
        else:
            job["title"] = el.get_text(strip=True)

    # Try to extract location / department from sibling/child text nodes
    text_nodes = [t.strip() for t in el.stripped_strings]
    for node in text_nodes[1:]:
        low = node.lower()
        if any(w in low for w in ["remote", "hybrid", "onsite", "office", "city", "new york", "london", "berlin", "paris", "san francisco"]):
            if not job["location"]:
                job["location"] = node
        elif any(w in low for w in ["engineering", "product", "design", "sales", "marketing", "finance", "operations", "people", "legal"]):
            if not job["department"]:
                job["department"] = node

    if not job["title"] or len(job["title"]) > 200:
        return None
    return job


# ---------------------------------------------------------------------------
# Playwright scraper (JS-rendered pages)
# ---------------------------------------------------------------------------

async def _scrape_playwright_async(url: str) -> JobList:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright is not installed. Run: pip install playwright && playwright install chromium")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(extra_http_headers=HEADERS)
        await page.goto(url, wait_until="networkidle", timeout=60_000)

        # Scroll to trigger lazy loading
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.5)

        # Handle "Load more" / pagination buttons
        for _ in range(10):
            load_more = page.locator(
                "button:has-text('Load more'), button:has-text('Show more'), "
                "button:has-text('View more'), a:has-text('Next'), "
                "[aria-label='Next page'], [data-automation='pagination-next']"
            )
            if await load_more.count() > 0:
                try:
                    await load_more.first.click()
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                    await asyncio.sleep(0.5)
                except Exception:
                    break
            else:
                break

        html = await page.content()
        await browser.close()

    return _parse_html_jobs(html, url)


def _scrape_playwright(url: str) -> JobList:
    return asyncio.run(_scrape_playwright_async(url))


# ---------------------------------------------------------------------------
# Single job detail
# ---------------------------------------------------------------------------

def scrape_job_detail(job_url: str) -> str:
    """Fetch the full description of a single job posting."""
    try:
        with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            resp = client.get(job_url)
            resp.raise_for_status()
            html = resp.text
    except Exception:
        # Fall back to Playwright
        html = _get_html_playwright(job_url)

    soup = BeautifulSoup(html, "html.parser")

    # Remove nav / header / footer noise
    for tag in soup.select("nav, header, footer, script, style, [role='navigation']"):
        tag.decompose()

    # Common job-detail content selectors
    for sel in [
        "[class*='job-description']",
        "[class*='description']",
        "[id*='description']",
        "article",
        "main",
        ".content",
        "#content",
    ]:
        el = soup.select_one(sel)
        if el:
            return el.get_text(separator="\n").strip()

    return soup.get_text(separator="\n").strip()


def _get_html_playwright(url: str) -> str:
    async def _inner() -> str:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(extra_http_headers=HEADERS)
            await page.goto(url, wait_until="networkidle", timeout=60_000)
            html = await page.content()
            await browser.close()
            return html
    return asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_jobs(url: str) -> JobList:
    """
    Scrape all job postings from a careers page URL.

    Detection order:
      1. Greenhouse API
      2. Lever API
      3. Static HTML (httpx + BS4)
      4. Playwright (JS-rendered)
    """
    slug = _is_greenhouse(url)
    if slug:
        return _scrape_greenhouse(slug)

    slug = _is_lever(url)
    if slug:
        return _scrape_lever(slug)

    # Try static first; fall back to Playwright if too few jobs found
    try:
        jobs = _scrape_static(url)
    except Exception:
        jobs = []

    if len(jobs) < 3:
        jobs = _scrape_playwright(url)

    return jobs
