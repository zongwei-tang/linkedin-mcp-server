"""Browser-DOM tests for telling Cloudflare's check apart from a LinkedIn page.

Measured 2026-11-21: LinkedIn's public routes answered a headless browser on a
datacenter host with Cloudflare's "Performing security verification" page on
the page's own URL. These load synthetic pages in headless chromium. The
check pages carry only the marks Cloudflare's protocol defines (its challenge
platform script, its ``__cf_chl_`` token) under German words, so nothing here
can pass by reading text: a claim about the algorithm, not about the copy.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.bot_challenge import page_is_challenge
from linkedin_mcp_server.core.exceptions import BotChallengeError
from linkedin_mcp_server.core.utils import detect_rate_limit

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

URL = "https://www.linkedin.com/pulse/engines-ada/"
CHECK = (
    "<title>Einen Moment bitte...</title>"
    "<body><h1>www.linkedin.com</h1><p>Sicherheitsüberprüfung läuft</p>"
    '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=a5"></script>'
    "</body>"
)
ARTICLE = (
    "<main><h1>Engines</h1><p>Einen Moment bitte: Sicherheitsüberprüfung "
    "ist ein Thema dieses Artikels.</p></main>"
)


@pytest.fixture
async def dom_page():
    async def answer(route):
        await route.fulfill(status=200, content_type="text/html", body="<p>ok</p>")

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            context = await browser.new_context()
            page = await context.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        await context.route("**/*", answer)
        try:
            yield page
        finally:
            await browser.close()


async def at(page, html: str, url: str = URL):
    await page.goto(url)
    await page.set_content(html)
    return page


async def test_the_check_page_is_recognised_by_its_platform_script(dom_page):
    assert await page_is_challenge(await at(dom_page, CHECK)) is True


async def test_an_article_about_security_checks_is_not_one(dom_page):
    assert await page_is_challenge(await at(dom_page, ARTICLE)) is False


async def test_the_token_cloudflare_adds_to_the_address_is_recognised(dom_page):
    page = await at(dom_page, "<body></body>", URL + "?__cf_chl_rt_tk=E1kn")
    assert await page_is_challenge(page) is True


async def test_a_read_refuses_the_check_page_instead_of_returning_its_text(dom_page):
    # Before: no <main>, short body, no English rate-limit phrase, so the
    # check's words came back as the article.
    with pytest.raises(BotChallengeError):
        await detect_rate_limit(await at(dom_page, CHECK))


async def test_a_read_of_a_real_page_goes_on(dom_page):
    await detect_rate_limit(await at(dom_page, ARTICLE))
