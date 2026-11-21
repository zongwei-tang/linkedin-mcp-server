"""Browser-DOM tests for following and unfollowing a company page.

``COMPANY_FOLLOW_JS`` never runs under the unit suite's mocked
``page.evaluate``. These drive ``CompanyActions.set_company_followed`` against
a synthetic company page in headless chromium, with navigation stubbed. The
page behaves as measured on 2026-11-20: the page's follow button is
``button[aria-pressed]`` around ``check-small`` (followed) or ``add-small``
beside an overflow ``button[aria-expanded]`` around
``overflow-web-ios-small``; a post's author Follow button is
``button[aria-pressed]`` around ``add-small`` with no overflow beside it; and
an unfollow asks first in a ``role="alertdialog"`` whose confirm carries
``data-test-dialog-primary-btn``. Labels are German on purpose.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin import company_actions
from linkedin_mcp_server.linkedin.company_actions import CompanyActions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

URL = "https://www.linkedin.com/company/acme/"

SCRIPT = """
const log = (what) => {
    const entries = JSON.parse(document.body.dataset.log || '[]');
    entries.push(what);
    document.body.dataset.log = JSON.stringify(entries);
};
const page = document.querySelector('[data-who="page"]');
const show = (followed) => {
    page.setAttribute('aria-pressed', String(followed));
    page.innerHTML = '<svg data-test-icon="' + (followed ? 'check-small' : 'add-small')
        + '"></svg>' + (followed ? 'Folge ich' : 'Folgen');
};
page.addEventListener('click', () => {
    log('page');
    if (document.body.hasAttribute('data-ignore')) return;
    if (page.getAttribute('aria-pressed') !== 'true') { show(true); return; }
    if (document.body.hasAttribute('data-no-ask')) { show(false); return; }
    const dialog = document.createElement('div');
    dialog.setAttribute('role', 'alertdialog');
    dialog.innerHTML = '<button data-test-modal-close-btn><svg data-test-icon="close-medium"></svg></button>'
        + '<h2>Seite entfolgen</h2>'
        + '<button data-test-dialog-secondary-btn>Abbrechen</button>'
        + (document.body.hasAttribute('data-no-confirm') ? ''
           : '<button data-test-dialog-primary-btn>Nicht mehr folgen</button>');
    dialog.addEventListener('click', (event) => {
        const button = event.target.closest('button');
        if (!button) return;
        if (button.hasAttribute('data-test-dialog-primary-btn')) { log('confirm'); show(false); }
        else log('cancel');
        dialog.remove();
    });
    document.body.appendChild(dialog);
});
for (const other of document.querySelectorAll('[data-who="author"]')) {
    other.addEventListener('click', () => log('author'));
}
"""


def company(followed: bool | None) -> str:
    top = ""
    if followed is not None:
        icon = "check-small" if followed else "add-small"
        top = (
            '<div role="region"><div><div>'
            f'<button aria-pressed="{str(followed).lower()}" aria-label="Folgen" data-who="page">'
            f'<svg data-test-icon="{icon}"></svg>Folgen</button></div>'
            '<button aria-expanded="false"><svg data-test-icon="overflow-web-ios-small"></svg></button>'
            "</div></div>"
        )
    return (
        f"<main>{top}"
        "<section><div><div>"
        '<button aria-expanded="false" aria-label="Menü für Beitrag"></button>'
        '<button aria-pressed="false" data-who="author">'
        '<svg data-test-icon="add-small"></svg>Folgen</button>'
        "</div></div></section>"
        f"</main><script>{SCRIPT}</script>"
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


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(company_actions, "_PAGE_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(company_actions, "_FOLLOW_TIMEOUT", 1.0)
    monkeypatch.setattr(company_actions, "NAV_DELAY", 0.0)


async def act(page, html: str, followed: bool, *, body_attrs=(), reload_html=None):
    await page.goto(URL)
    await page.set_content(html)
    for name in body_attrs:
        await page.evaluate("(n) => document.body.setAttribute(n, '')", name)
    session = PageSession(page)
    actions = CompanyActions(session, PageNavigator(session))
    loads = 0

    async def navigate(url: str) -> None:
        nonlocal loads
        loads += 1
        if loads > 1 and reload_html is not None:
            await page.set_content(reload_html)

    with (
        patch.object(actions._navigator, "_navigate_to_page", side_effect=navigate),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
    ):
        return await actions.set_company_followed("acme", followed)


async def log(page) -> list[str]:
    return await page.evaluate("() => JSON.parse(document.body.dataset.log || '[]')")


async def test_following_is_one_click_on_the_pages_button(dom_page):
    result = await act(dom_page, company(False), True)

    assert result == {
        "url": URL,
        "company": "acme",
        "followed": True,
        "status": "followed",
    }
    assert await log(dom_page) == ["page"]


async def test_unfollowing_confirms_with_the_primary_button(dom_page):
    result = await act(dom_page, company(True), False)

    assert result["status"] == "unfollowed"
    assert await log(dom_page) == ["page", "confirm"]


async def test_an_unfollow_that_does_not_ask_is_still_confirmed(dom_page):
    result = await act(dom_page, company(True), False, body_attrs=["data-no-ask"])

    assert result["status"] == "unfollowed"
    assert await log(dom_page) == ["page"]


async def test_a_followed_page_is_left_alone(dom_page):
    result = await act(dom_page, company(True), True)

    assert result["status"] == "already_followed"
    assert await log(dom_page) == []


async def test_an_authors_follow_button_never_stands_in_for_the_pages(dom_page):
    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, company(None), True)
    assert await log(dom_page) == []


async def test_a_confirmation_without_its_confirm_button_is_closed(dom_page):
    with pytest.raises(LinkedInOperationError, match="nothing was confirmed"):
        await act(dom_page, company(True), False, body_attrs=["data-no-confirm"])
    assert "confirm" not in await log(dom_page)


async def test_a_click_that_changes_nothing_is_an_error(dom_page):
    # Clicked, so the account may have changed: an unknown outcome.
    with pytest.raises(ActionOutcomeError, match="did not change"):
        await act(dom_page, company(False), True, body_attrs=["data-ignore"])


async def test_a_follow_the_reload_does_not_show_is_an_error(dom_page):
    with pytest.raises(ActionOutcomeError, match="not after a reload"):
        await act(dom_page, company(False), True, reload_html=company(False))


async def test_a_follow_button_missing_after_the_click_is_an_unknown_outcome(
    dom_page,
):
    # The click went through: saying nothing was clicked would be wrong.
    with pytest.raises(ActionOutcomeError) as caught:
        await act(dom_page, company(False), True, reload_html=company(None))
    assert "Nothing was clicked" not in str(caught.value)
