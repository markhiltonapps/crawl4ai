"""
Tests for the whole-site Markdown export (site_export.py + /site-export routes).

The helpers are pure and tested directly. The routes are exercised through the
real app without its lifespan (no browser), so they cover auth, the deep-crawl
opt-in, SSRF validation and downloads of a finished job.
"""

import json
import zipfile
import io

import pytest

import site_export as se

SITE = "https://www.example.com/"
TOKEN = "test-site-export-token"


# ───────────────────────────── URL helpers ─────────────────────────────

class TestUrls:
    host = se.site_host(SITE)

    def test_relative_link_resolves(self):
        assert se.normalize_url("/about/", SITE + "x/", self.host) == "https://www.example.com/about/"

    def test_fragment_and_query_dropped(self):
        assert se.normalize_url("/faq/?ref=nav#top", SITE, self.host) == "https://www.example.com/faq/"

    def test_www_and_bare_host_are_same_site(self):
        assert se.normalize_url("https://example.com/blog/", SITE, self.host) == "https://example.com/blog/"

    @pytest.mark.parametrize("href", [
        "mailto:a@b.com", "tel:+18885551234", "javascript:void(0)", "#main",
        "https://other.com/page/", "https://cdn.example.org/x/",
        "/files/brochure.PDF", "/img/logo.png", "ftp://example.com/x",
    ])
    def test_non_pages_rejected(self, href):
        assert se.normalize_url(href, SITE, self.host) is None

    def test_trailing_slash_and_www_share_a_key(self):
        assert se.page_key("https://www.example.com/about") == se.page_key("https://example.com/about/")
        assert se.page_key("https://example.com") == se.page_key("https://example.com/")


# ─────────────────────────────── FAQs ───────────────────────────────

def _jsonld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


class TestFaqs:
    def test_faqpage_inside_graph_with_html_answers(self):
        page = _jsonld({"@graph": [
            {"@type": "Organization", "name": "X"},
            {"@type": "FAQPage", "mainEntity": [
                {"@type": "Question", "name": "Do you take Medicare?",
                 "acceptedAnswer": {"@type": "Answer", "text": "<p>Yes &amp; most plans.</p>"}},
                {"@type": "Question", "name": "Hours?",
                 "acceptedAnswer": [{"@type": "Answer", "text": "8-4"}]},
            ]},
        ]})
        assert se.extract_faqs(page) == [("Do you take Medicare?", "Yes & most plans."), ("Hours?", "8-4")]

    def test_bad_json_and_duplicates_ignored(self):
        q = {"@type": "Question", "name": "Q1", "acceptedAnswer": {"text": "A1"}}
        page = '<script type="application/ld+json">{not json</script>' + _jsonld(q) + _jsonld(q)
        assert se.extract_faqs(page) == [("Q1", "A1")]

    def test_no_jsonld(self):
        assert se.extract_faqs("<html><body>hi</body></html>") == []


# ───────────────────────────── cleaning ─────────────────────────────

BANNER = "Now accepting new patients. [Book](https://www.example.com/book/)"


def _page(path, body, faqs=()):
    return se.Page(SITE + path, f"{BANNER}\n[Skip to content](#main)\n{body}", list(faqs))


class TestCleaning:
    def test_answer_placed_under_collapsed_question(self):
        page = se.Page(SITE + "faq/", "# FAQ\n### Is it covered?\n+\n### Other?\n+", [
            ("Is it covered?", "Yes, by Medicare."), ("Not on page?", "Appended."),
        ])
        text = se.clean_page(page, set(), keep_boilerplate=False)
        assert "### Is it covered?\nYes, by Medicare." in text
        assert "\n+\n" not in text and not text.endswith("+")
        assert "### More questions and answers" in text and "**Not on page?**\nAppended." in text

    def test_visible_answer_not_duplicated(self):
        page = se.Page(SITE, "### Q?\nThe full visible answer text is already here on the page.",
                       [("Q?", "The full visible answer text is already here on the page.")])
        text = se.clean_page(page, set(), keep_boilerplate=True)
        assert text.count("The full visible answer") == 1

    def test_counters_and_cookie_banner_dropped(self):
        page = se.Page(SITE, "# Home\n0+Texas Locations\nWe use cookies. [Privacy](x)\nDeclineAccept\nReal text", [])
        assert se.clean_page(page, set(), keep_boilerplate=True) == "# Home\nReal text"
        spaced = se.Page(SITE, "# Home\nDecline Accept\nReal text", [])
        assert se.clean_page(spaced, set(), keep_boilerplate=True) == "# Home\nReal text"


SPACING_FIXTURE = """<html><body>
<div style="display:flex;justify-content:space-between;width:300px"><span id="d">Sat - Sun</span><span>Closed</span></div>
<address id="addr"><span>919 West Randol Mill Rd<br></span><span>Main Building<br></span><span>Arlington, TX</span></address>
<p id="word"><b>Hel</b><i>lo</i> world</p>
<p id="hidden"><span>A</span><span style="display:none">B</span></p>
</body></html>"""


class TestSpacingScript:
    """SPACING_JS in a real browser; skipped where none is installed."""

    @pytest.fixture(scope="class")
    def page(self):
        sync_api = pytest.importorskip("playwright.sync_api")
        try:
            pw = sync_api.sync_playwright().start()
            browser = pw.chromium.launch()
        except Exception as e:
            pytest.skip(f"no browser available: {e}")
        page = browser.new_page()
        page.set_content(SPACING_FIXTURE)
        page.evaluate(se.SPACING_JS)
        yield page
        browser.close()
        pw.stop()

    def test_spaced_flex_pair(self, page):
        assert page.text_content("div").strip() == "Sat - Sun Closed"

    def test_stacked_spans(self, page):
        assert " ".join(page.text_content("#addr").split()) == "919 West Randol Mill Rd Main Building Arlington, TX"

    def test_touching_inline_elements_untouched(self, page):
        assert page.text_content("#word") == "Hello world"

    def test_hidden_elements_untouched(self, page):
        assert page.text_content("#hidden") == "AB"

    def test_sitewide_lines_kept_only_on_start_page(self):
        body = "x" * 250
        pages = [_page("", f"# Home\n{body}")] + [_page(f"p{i}/", f"# Page {i}\n{body}{i}") for i in range(4)]
        kept, dropped = se.build_documents(pages, SITE)
        assert dropped == []
        assert kept[0].url == SITE and BANNER in kept[0].markdown
        assert all(BANNER not in p.markdown and "Skip to content" not in p.markdown for p in kept[1:])
        assert [p.title for p in kept[1:]] == ["Page 0", "Page 1", "Page 2", "Page 3"]

    def test_small_sites_keep_everything(self):
        pages = [_page("", "# Home\n" + "a" * 250), _page("b/", "# B\n" + "b" * 250)]
        kept, _ = se.build_documents(pages, SITE)
        assert all(BANNER in p.markdown for p in kept)

    def test_near_empty_pages_dropped_but_start_page_kept(self):
        pages = [_page("", "# Home"), _page("login/", "# Log in"), _page("about/", "# About\n" + "a" * 300)]
        kept, dropped = se.build_documents(pages, SITE)
        assert [p.url for p in kept] == [SITE, SITE + "about/"]
        assert dropped == [SITE + "login/"]


class TestRendering:
    def test_markdown_has_one_section_per_page(self):
        pages = [se.Page(SITE, "# Home\nhi", [], "Home"), se.Page(SITE + "a/", "# A\nyo", [], "A")]
        md = se.render_markdown(pages, SITE, "2026-01-01")
        assert md.startswith("# Website content: example.com\n")
        assert "## PAGE: Home\nURL: https://www.example.com/\n" in md
        assert md.count("## PAGE:") == 2

    def test_zip_names_are_unique_and_safe(self):
        pages = [se.Page(SITE, "a", [], "Home"), se.Page(SITE + "blog/x/", "b", [], "X"),
                 se.Page(SITE + "blog/x", "c", [], "X again"), se.Page(SITE + "a b/?q", "d", [], "Odd")]
        names = zipfile.ZipFile(io.BytesIO(se.render_zip(pages))).namelist()
        assert names == ["home.md", "blog__x.md", "blog__x-2.md", "a-b.md"]

    def test_linked_title_is_plain_text(self):
        assert se.page_title("#  [Quotes to Scrape](https://q.com/)\nx", "fb") == "Quotes to Scrape"
        assert se.page_title("no heading", "fb") == "fb"

    def test_filename(self):
        assert se.export_filename("https://www.New-Promise.com/x", "md") == "new-promise-com-content.md"


# ─────────────────────────────── routes ───────────────────────────────

@pytest.fixture
def client(stock_client, monkeypatch):
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", TOKEN)
    monkeypatch.setenv("CRAWL4AI_ALLOW_DEEP_CRAWL", "true")
    se.JOBS.clear()
    yield stock_client
    se.JOBS.clear()


AUTH = {"Authorization": f"Bearer {TOKEN}"}


class TestRoutes:
    def test_requires_token(self, client):
        assert client.post("/site-export", json={"url": SITE}).status_code == 401
        assert client.get("/site-export/abc").status_code == 401

    def test_requires_deep_crawl_opt_in(self, client, monkeypatch):
        monkeypatch.setenv("CRAWL4AI_ALLOW_DEEP_CRAWL", "false")
        r = client.post("/site-export", json={"url": SITE}, headers=AUTH)
        assert r.status_code == 403
        assert not se.JOBS

    @pytest.mark.parametrize("url", ["http://127.0.0.1:6379/", "http://169.254.169.254/latest/"])
    def test_internal_destinations_blocked(self, client, url):
        r = client.post("/site-export", json={"url": url}, headers=AUTH)
        assert r.status_code == 400
        assert not se.JOBS

    def test_one_export_at_a_time(self, client):
        se.create_job(SITE, 10)  # state "running"
        r = client.post("/site-export", json={"url": SITE}, headers=AUTH)
        assert r.status_code == 409

    def test_status_and_downloads_of_finished_job(self, client):
        job = se.create_job(SITE, 10)
        job.state, job.markdown, job.zip_bytes = "done", "# Website content\n", b"PK\x05\x06" + b"\0" * 18
        status = client.get(f"/site-export/{job.id}", headers=AUTH).json()
        assert status["state"] == "done" and "markdown" not in status

        md = client.get(f"/site-export/{job.id}/download", headers=AUTH)
        assert md.status_code == 200 and md.text == "# Website content\n"
        assert md.headers["content-type"].startswith("text/markdown")
        assert 'filename="example-com-content.md"' in md.headers["content-disposition"]

        zp = client.get(f"/site-export/{job.id}/download?format=zip", headers=AUTH)
        assert zp.status_code == 200 and zp.headers["content-type"] == "application/zip"
        assert client.get(f"/site-export/{job.id}/download?format=exe", headers=AUTH).status_code == 400

    def test_download_needs_token_and_finished_job(self, client):
        job = se.create_job(SITE, 10)
        assert client.get(f"/site-export/{job.id}/download").status_code == 401
        assert client.get(f"/site-export/{job.id}/download", headers=AUTH).status_code == 409
        assert client.get("/site-export/nope/download", headers=AUTH).status_code == 404

    def test_page_is_served_without_token(self, client):
        r = client.get("/knowledge/")
        assert r.status_code == 200 and "Download .md file" in r.text
