"""
site_export.py - crawl a whole website into one clean Markdown knowledge file.

Built for "give my AI agent everything on this site": starting from one URL it
follows same-site links, takes the fit (boilerplate-pruned) Markdown of every
page, and then tidies the result so it reads well as a single document:

  * FAQ answers hidden in collapsed accordions are recovered from the page's
    schema.org FAQPage JSON-LD and placed under their questions;
  * lines repeated on many pages (banners, booking widgets, cookie notices)
    are kept once, on the start page, instead of on every page;
  * animated counters captured before they ran ("0+ Locations") are dropped.

Jobs run in the background (an export can take minutes) and are held in memory
by this single gunicorn worker. The pure helpers below are unit-tested in
tests/test_site_export.py; run_export() is the only part that needs a browser.
"""

from __future__ import annotations

import asyncio
import collections
import html as html_lib
import io
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from uuid import uuid4

logger = logging.getLogger(__name__)

# Paths that are files, not pages.
_SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".zip", ".gz", ".rar", ".mp3", ".mp4", ".mov", ".avi", ".webm", ".wav",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
    ".css", ".js", ".json", ".xml", ".rss", ".txt",
)
# Lines that are page chrome rather than content.
_NOISE_LINE = re.compile(
    r"^(\[Skip to (main )?content\]|We use cookies|Decline ?Accept$|Accept( all)?$|"
    r"0\+|[+×]$)",
    re.IGNORECASE,
)
_JSONLD_BLOCK = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I
)
# Runs in the page before capture. Sites often lay out label/value pairs as
# adjacent elements with no whitespace between them in the markup
# (<span>Sat - Sun</span><span>Closed</span> in a flex row), so the extracted
# text reads "Sat - SunClosed". Where two sibling elements are drawn apart on
# screen - on different lines, or with a gap between them - add the space the
# layout implies. Elements that touch on the same line (<b>Hel</b><i>lo</i>)
# are left alone, as are hidden ones.
SPACING_JS = """
(() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'BR', 'WBR']);
  for (const el of Array.from(document.body.querySelectorAll('*'))) {
    const next = el.nextSibling;
    if (!next || next.nodeType !== 1 || SKIP.has(el.tagName) || SKIP.has(next.tagName)) continue;
    const a = el.getBoundingClientRect(), b = next.getBoundingClientRect();
    if (!(a.width || a.height) || !(b.width || b.height)) continue;
    const sameLine = b.top < a.bottom - 1 && a.top < b.bottom - 1;
    if (sameLine && Math.abs(b.left - a.right) < 1) continue;
    el.after(document.createTextNode(' '));
  }
})();
"""
# A line on at least this share of pages is treated as site-wide boilerplate.
REPEAT_SHARE = 0.25
# Below this many pages there is no meaningful "site-wide" signal.
REPEAT_MIN_PAGES = 4
# A page whose cleaned text is shorter than this (login screens, bare forms)
# is left out, except the start page.
MIN_PAGE_CHARS = 200


# ───────────────────────────── URL helpers ─────────────────────────────

def site_host(url: str) -> str:
    """Host used to decide whether a link is on the same site (www-insensitive)."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(href: str, base: str, host: str) -> Optional[str]:
    """Absolute, fragment- and query-free URL for a same-site page, else None."""
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
        return None
    url, _ = urldefrag(urljoin(base, href))
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if site_host(url) != host:
        return None
    path = parts.path or "/"
    if path.lower().endswith(_SKIP_EXTENSIONS):
        return None
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def page_key(url: str) -> str:
    """Identity of a page: /about and /about/ (and www/no-www) are the same page."""
    parts = urlsplit(url)
    return site_host(url) + (parts.path.rstrip("/") or "/")


# ─────────────────────────── content helpers ───────────────────────────

def extract_faqs(page_html: str) -> List[Tuple[str, str]]:
    """(question, answer) pairs from schema.org Question objects in JSON-LD.

    Sites put FAQ answers here for search engines even when the visible copy
    sits in a collapsed accordion the crawler never opens.
    """
    pairs: List[Tuple[str, str]] = []
    for block in _JSONLD_BLOCK.findall(page_html or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(reversed(node))
                continue
            if not isinstance(node, dict):
                continue
            if node.get("@type") == "Question":
                answer = node.get("acceptedAnswer") or {}
                if isinstance(answer, list):
                    answer = answer[0] if answer else {}
                question = html_lib.unescape(str(node.get("name", ""))).strip()
                text = html_lib.unescape(str(answer.get("text", "")))
                text = re.sub(r"<[^>]+>", "", text).strip()
                if question and text:
                    pairs.append((question, text))
            stack.extend(reversed([v for v in node.values() if isinstance(v, (dict, list))]))
    seen = set()
    return [(q, a) for q, a in pairs if not (q in seen or seen.add(q))]


def page_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line[2:]).strip()
            return title or fallback
    return fallback


@dataclass
class Page:
    url: str
    markdown: str
    faqs: List[Tuple[str, str]] = field(default_factory=list)
    title: str = ""


def repeated_lines(pages: Iterable[Page]) -> set:
    pages = list(pages)
    if len(pages) < REPEAT_MIN_PAGES:
        return set()
    counts = collections.Counter()
    for page in pages:
        counts.update({l.strip() for l in page.markdown.splitlines() if l.strip()})
    return {line for line, n in counts.items() if n >= len(pages) * REPEAT_SHARE}


def clean_page(page: Page, boilerplate: set, keep_boilerplate: bool) -> str:
    """Drop chrome and site-wide lines; put each FAQ answer under its question."""
    answers = dict(page.faqs)
    used = set()
    out: List[str] = []
    for line in page.markdown.splitlines():
        stripped = line.strip()
        if _NOISE_LINE.match(stripped):
            continue
        if not keep_boilerplate and stripped in boilerplate:
            continue
        out.append(line)
        question = re.sub(r"^#+\s*", "", stripped)
        if question in answers and question not in used:
            used.add(question)
            if answers[question][:60] not in page.markdown:
                out.append(answers[question])
    leftover = [(q, a) for q, a in page.faqs
                if q not in used and a[:60] not in page.markdown]
    if leftover:
        out.append("\n### More questions and answers")
        for q, a in leftover:
            out += [f"**{q}**", a, ""]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def build_documents(pages: List[Page], start_url: str) -> Tuple[List[Page], List[str]]:
    """Clean every page. Returns (kept pages, URLs dropped for having no content)."""
    boilerplate = repeated_lines(pages)
    start = page_key(start_url)
    kept, dropped = [], []
    for page in pages:
        is_start = page_key(page.url) == start
        text = clean_page(page, boilerplate, keep_boilerplate=is_start)
        if len(text) < MIN_PAGE_CHARS and not is_start:
            dropped.append(page.url)
            continue
        kept.append(Page(page.url, text, page.faqs, page_title(text, page.url)))
    kept.sort(key=lambda p: (page_key(p.url) != start, p.url))
    return kept, dropped


def render_markdown(pages: List[Page], start_url: str, crawled_at: str) -> str:
    parts = [
        f"# Website content: {site_host(start_url)}\n\n"
        f"Source: {start_url} (crawled {crawled_at}, {len(pages)} pages)\n"
    ]
    for page in pages:
        parts.append(f"\n\n---\n\n## PAGE: {page.title}\nURL: {page.url}\n\n{page.markdown}")
    return "".join(parts) + "\n"


def render_zip(pages: List[Page]) -> bytes:
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for page in pages:
            slug = urlsplit(page.url).path.strip("/").replace("/", "__") or "home"
            slug = re.sub(r"[^A-Za-z0-9_.-]", "-", slug)[:120]
            name, n = slug, 2
            while name in used:
                name, n = f"{slug}-{n}", n + 1
            used.add(name)
            zf.writestr(f"{name}.md", f"# {page.title}\nURL: {page.url}\n\n{page.markdown}\n")
    return buf.getvalue()


def export_filename(start_url: str, ext: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", site_host(start_url)).strip("-") or "site"
    return f"{stem}-content.{ext}"


# ─────────────────────────────── jobs ───────────────────────────────

JOB_TTL_SEC = 3600
MAX_KEPT_JOBS = 5


@dataclass
class ExportJob:
    id: str
    start_url: str
    max_pages: int
    state: str = "running"          # running | done | failed
    pages_found: int = 1
    pages_crawled: int = 0
    current_url: str = ""
    error: str = ""
    failed_urls: List[str] = field(default_factory=list)
    dropped_urls: List[str] = field(default_factory=list)
    page_count: int = 0
    word_count: int = 0
    markdown: str = ""
    zip_bytes: bytes = b""
    created: float = field(default_factory=time.time)
    finished: float = 0.0
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    def status(self) -> Dict:
        return {
            "job_id": self.id,
            "state": self.state,
            "start_url": self.start_url,
            "max_pages": self.max_pages,
            "pages_found": self.pages_found,
            "pages_crawled": self.pages_crawled,
            "current_url": self.current_url,
            "error": self.error,
            "failed_urls": self.failed_urls,
            "dropped_urls": self.dropped_urls,
            "page_count": self.page_count,
            "word_count": self.word_count,
        }


JOBS: Dict[str, ExportJob] = {}


def _prune_jobs() -> None:
    now = time.time()
    for job_id, job in list(JOBS.items()):
        if job.state != "running" and now - job.finished > JOB_TTL_SEC:
            del JOBS[job_id]
    finished = sorted((j for j in JOBS.values() if j.state != "running"),
                      key=lambda j: j.finished)
    for job in finished[:-MAX_KEPT_JOBS]:
        del JOBS[job.id]


def running_job() -> Optional[ExportJob]:
    return next((j for j in JOBS.values() if j.state == "running"), None)


def create_job(start_url: str, max_pages: int) -> ExportJob:
    _prune_jobs()
    job = ExportJob(id=uuid4().hex, start_url=start_url, max_pages=max_pages)
    JOBS[job.id] = job
    return job


async def run_export(job: ExportJob, concurrency: int = 4, deadline_sec: float = 2700) -> None:
    """Breadth-first crawl of the site, then clean and render. Never raises."""
    from crawl4ai import BrowserConfig, CacheMode, CrawlerRunConfig
    from crawl4ai.content_filter_strategy_lxml import PruningContentFilterLXML
    from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    from crawler_pool import get_crawler, release_crawler
    from egress_broker import enforce_egress
    from utils import load_config, validate_url_destination

    crawler = None
    try:
        cfg = load_config()
        browser_cfg = BrowserConfig(
            extra_args=cfg["crawler"]["browser"].get("extra_args", []),
            **cfg["crawler"]["browser"].get("kwargs", {}),
        )
        enforce_egress(browser_cfg)
        crawler = await get_crawler(browser_cfg)
        run_cfg = CrawlerRunConfig(
            markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilterLXML()),
            scraping_strategy=LXMLWebScrapingStrategy(),
            cache_mode=CacheMode.BYPASS,
            js_code=SPACING_JS,
        )

        host = site_host(job.start_url)
        queue = collections.deque([job.start_url])
        seen = {page_key(job.start_url)}
        pages: List[Page] = []
        deadline = time.monotonic() + deadline_sec
        sem = asyncio.Semaphore(concurrency)

        first_error = ""

        async def fetch(url: str):
            nonlocal first_error
            async with sem:
                job.current_url = url
                try:
                    validate_url_destination(url)
                    result = await crawler.arun(url=url, config=run_cfg)
                    if not result.success:
                        first_error = first_error or (result.error_message or "").strip()
                    return url, result
                except Exception as e:  # one bad page must not sink the export
                    logger.warning("site export: %s failed: %s", url, e)
                    first_error = first_error or str(getattr(e, "detail", None) or e)
                    return url, None

        while queue and job.pages_crawled < job.max_pages:
            if time.monotonic() > deadline:
                logger.warning("site export %s hit its deadline", job.id)
                break
            batch = [queue.popleft()
                     for _ in range(min(len(queue), concurrency * 2,
                                        job.max_pages - job.pages_crawled))]
            for url, result in await asyncio.gather(*(fetch(u) for u in batch)):
                job.pages_crawled += 1
                if result is None or not result.success:
                    job.failed_urls.append(url)
                    continue
                final_url = result.redirected_url or url
                if site_host(final_url) != host:
                    job.failed_urls.append(url)
                    continue
                md = result.markdown.fit_markdown if result.markdown else ""
                pages.append(Page(url, md or "", extract_faqs(result.html)))
                for link in (result.links or {}).get("internal", []):
                    nxt = normalize_url(link.get("href", ""), final_url, host)
                    if nxt and page_key(nxt) not in seen:
                        seen.add(page_key(nxt))
                        queue.append(nxt)
                job.pages_found = len(seen)

        if not pages:
            reason = first_error.splitlines()[0][:300] if first_error else "no response"
            raise RuntimeError(f"Could not load {job.start_url}: {reason}")
        kept, dropped = build_documents(pages, job.start_url)
        job.dropped_urls = dropped
        crawled_at = time.strftime("%Y-%m-%d", time.gmtime())
        job.markdown = render_markdown(kept, job.start_url, crawled_at)
        job.zip_bytes = render_zip(kept)
        job.page_count = len(kept)
        job.word_count = len(job.markdown.split())
        job.state = "done"
    except Exception as e:
        logger.error("site export %s failed: %s", job.id, e, exc_info=True)
        job.error = getattr(e, "detail", None) or str(e)
        job.state = "failed"
    finally:
        job.current_url = ""
        job.finished = time.time()
        if crawler:
            await release_crawler(crawler)
