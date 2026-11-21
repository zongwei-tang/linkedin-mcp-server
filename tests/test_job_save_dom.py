"""Browser-DOM tests for saving and unsaving a job posting.

``SAVE_BUTTON_JS`` never runs under the unit suite's mocked ``page.evaluate``.
These drive ``JobActions.set_job_saved`` against a synthetic posting in
headless chromium, with navigation stubbed and the saved-jobs list played by
a fake that follows the page. The posting behaves as measured on 2026-11-20:
the apply link (the safety redirect, or the posting's `/apply` path) shares a
row with exactly one other button, Save, whose only change when clicked is its
words. Labels are German on purpose.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin import job_actions
from linkedin_mcp_server.linkedin.job_actions import JobActions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

JOB = "4493253527"
URL = f"https://www.linkedin.com/jobs/view/{JOB}/"

# A click on Save flips its words and records itself, unless told to ignore it.
SCRIPT = """
const save = document.querySelector('[data-who="save"]');
save.addEventListener('click', () => {
    const clicks = Number(document.body.dataset.clicks || '0') + 1;
    document.body.dataset.clicks = String(clicks);
    if (document.body.hasAttribute('data-ignore')) return;
    const saved = save.dataset.saved !== 'true';
    save.dataset.saved = String(saved);
    save.textContent = saved ? 'Gespeichert' : 'Speichern';
});
"""


def posting(
    *, saved: bool = False, apply: str = "/safety/go/?url=x", extra: str = ""
) -> str:
    words = "Gespeichert" if saved else "Speichern"
    return (
        "<main><div><div>"
        '<div><a href="https://www.linkedin.com/company/acme/">Acme</a>'
        '<button aria-expanded="false" aria-label="Weitere Optionen"></button></div>'
        "<div><div><div>"
        f'<div><a href="https://www.linkedin.com{apply}" aria-label="Bewerben">Bewerben</a></div>'
        f'<div><button data-who="save" data-saved="{str(saved).lower()}">{words}</button></div>'
        f"{extra}"
        "</div></div></div>"
        "</div></div>"
        '<section><button aria-label="Acme folgen">Folgen</button></section>'
        f"</main><script>{SCRIPT}</script>"
    )


class SavedList:
    """The saved-jobs list as the tracker reads it: what the page says, or a
    posting saved long ago that the pages read never reach."""

    def __init__(self, page, *, deep: bool = False):
        self.page = page
        self.deep = deep
        self.reads = 0

    async def __call__(self) -> list[str]:
        self.reads += 1
        saved = await self.page.evaluate(
            "() => document.querySelector('[data-who=\"save\"]').dataset.saved === 'true'"
        )
        if not saved:
            return ["111"]
        # A deep-saved posting is not on the pages read until saved again.
        return ["111"] if self.deep and self.reads == 1 else [JOB, "111"]


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


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(job_actions, "_POSTING_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(job_actions, "_TOGGLE_TIMEOUT", 1.0)
    monkeypatch.setattr(job_actions, "NAV_DELAY", 0.0)


async def act(
    page, html: str, saved: bool, *, deep: bool = False, ignore: bool = False
):
    await page.goto(URL)
    await page.set_content(html)
    if ignore:
        await page.evaluate("() => document.body.setAttribute('data-ignore', '')")
    session = PageSession(page)
    actions = JobActions(session, PageNavigator(session), SavedList(page, deep=deep))
    with (
        patch.object(actions._navigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
    ):
        return await actions.set_job_saved(JOB, saved)


async def clicks(page) -> int:
    return int(await page.evaluate("() => document.body.dataset.clicks || '0'"))


async def test_saving_clicks_the_button_beside_the_apply_link(dom_page):
    result = await act(dom_page, posting(), True)

    assert result == {"url": URL, "job_id": JOB, "saved": True, "status": "saved"}
    assert await clicks(dom_page) == 1


async def test_an_easy_apply_posting_is_found_by_its_apply_path(dom_page):
    html = posting(apply=f"/jobs/view/{JOB}/apply/?openSDUIApplyFlow=true")

    result = await act(dom_page, html, True)

    assert result["status"] == "saved"


async def test_a_listed_posting_is_not_saved_again(dom_page):
    result = await act(dom_page, posting(saved=True), True)

    assert result["status"] == "already_saved"
    assert await clicks(dom_page) == 0


async def test_unsaving_a_listed_posting(dom_page):
    result = await act(dom_page, posting(saved=True), False)

    assert result["status"] == "unsaved"
    assert await clicks(dom_page) == 1


async def test_unsaving_an_unsaved_posting_puts_back_the_save_it_made(dom_page):
    # Not listed, so it might be saved past the pages read: one click is
    # tried, it saves, and a second click puts the posting back as found.
    result = await act(dom_page, posting(saved=False), False)

    assert result["status"] == "already_unsaved"
    assert await clicks(dom_page) == 2


async def test_saving_a_posting_saved_past_the_pages_read_restores_it(dom_page):
    result = await act(dom_page, posting(saved=True), True, deep=True)

    assert result["status"] == "already_saved"
    assert await clicks(dom_page) == 2


async def test_a_row_with_two_buttons_is_not_clicked(dom_page):
    html = posting(extra="<div><button>Teilen</button></div>")

    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, True)
    assert await clicks(dom_page) == 0


async def test_a_posting_without_an_apply_link_is_not_clicked(dom_page):
    html = posting(apply="/company/acme/")

    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, True)
    assert await clicks(dom_page) == 0


async def test_a_click_that_changes_nothing_is_an_error(dom_page):
    with pytest.raises(LinkedInOperationError, match="did not change"):
        await act(dom_page, posting(), True, ignore=True)
