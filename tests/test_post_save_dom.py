"""Browser-DOM tests for saving and unsaving a post.

``POST_MENU_JS`` and ``SAVE_ITEM_JS`` never run under the unit suite's mocked
``page.evaluate``. These drive ``PostSaveActions.set_post_saved`` against a
synthetic post page in headless chromium, with navigation stubbed. The page
carries the signals measured on 2026-11-22 and nothing else: the post's control
menu button (``aria-expanded``, an ``aria-label``, no icon id) ahead of its
reactions caret (``data-finite-scroll-hotkey="l"``), comments as
``article[data-id]`` with menus of their own, and a menu whose items are
``role="button"`` around a ``data-test-icon``, the save item around
``bookmark-outline-medium`` or, once saved, ``bookmark-fill-medium``. Labels are
German on purpose: none of them may matter.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin import post_save_actions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_save_actions import PostSaveActions
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

POST = "urn:li:activity:7212345678901234567"
POST_URL = f"https://www.linkedin.com/feed/update/{POST}/"
#: Every choice made on every load of the post during one act(), in order.
LOG: list[str] = []

# The menu opens on its button, lists its items, and closes on Escape or on a
# choice. Choosing the bookmark item flips the saved state unless told to
# ignore it. Every choice is logged on the body, because patchright evaluates
# in an isolated world.
# Scoped, so a reload into the same window does not redeclare it.
SCRIPT = """(() => {
const log = (what) => {
    const entries = JSON.parse(document.body.dataset.log || '[]');
    entries.push(what);
    document.body.dataset.log = JSON.stringify(entries);
};
const item = (icon, words, who) =>
    '<li><div role="button" data-who="' + who + '"><svg data-test-icon="' + icon
    + '"></svg><h5>' + words + '</h5></div></li>';
const openMenu = (button) => {
    const saved = document.body.dataset.saved === 'true';
    const menu = document.createElement('div');
    menu.className = 'menu';
    menu.innerHTML = '<ul>'
        + (document.body.hasAttribute('data-no-save') ? ''
           : item(saved ? 'bookmark-fill-medium' : 'bookmark-outline-medium',
                  saved ? 'Nicht mehr speichern' : 'Speichern', 'save'))
        + item('link-medium', 'Link kopieren', 'link')
        + item('report-medium', 'Beitrag melden', 'report') + '</ul>';
    menu.addEventListener('click', (event) => {
        const who = event.target.closest('[role="button"]')?.dataset.who;
        if (!who) return;
        log(who);
        if (who === 'save' && !document.body.hasAttribute('data-ignore')) {
            document.body.dataset.saved = String(!saved);
        }
        menu.remove();
    });
    document.body.appendChild(menu);
};
for (const button of document.querySelectorAll('button[aria-expanded]')) {
    button.addEventListener('click', () => {
        log('menu:' + (button.dataset.who || '?'));
        openMenu(button);
    });
}
document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    log('escape');
    for (const menu of document.querySelectorAll('.menu')) menu.remove();
});
})();
"""


def comment(who: str) -> str:
    return (
        f'<article data-id="urn:li:comment:{who}"><p>Kommentar</p>'
        f'<button aria-expanded="false" aria-label="Optionen" data-who="{who}">'
        "</button>"
        '<button data-finite-scroll-hotkey="l" aria-expanded="false"></button>'
        "</article>"
    )


def post_page(saved: bool = False, *, menu: bool = True) -> str:
    control = (
        '<button aria-expanded="false" aria-label="Steuerungsmenü für Beitrag" '
        'data-who="post"></button>'
        if menu
        else ""
    )
    return (
        f'<main><div data-urn="{POST}">'
        '<button aria-expanded="false" aria-label="42 weitere Reaktionen" '
        'data-jump-link-target="reactors" data-who="jump"></button>'
        f"{control}<p>Beitrag</p>"
        '<div><button aria-pressed="false">Gefällt mir</button>'
        '<button data-finite-scroll-hotkey="l" aria-expanded="false" '
        'aria-label="Reaktionsmenü öffnen"></button></div></div>'
        f"<section>{comment('c1')}{comment('c2')}</section></main>"
        f"<script>document.body.dataset.saved = '{str(saved).lower()}';"
        f"{SCRIPT}</script>"
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
    monkeypatch.setattr(post_save_actions, "_POST_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(post_save_actions, "_MENU_TIMEOUT", 1.0)
    monkeypatch.setattr(post_save_actions, "NAV_DELAY", 0.0)


async def act(
    page,
    html: str,
    saved: bool,
    *,
    body_attrs=(),
    reload_html: str | None = None,
    reload_attrs=(),
):
    """Run one save or unsave.

    A reload serves the post as LinkedIn kept it (the state the page's own
    clicks left), or ``reload_html`` when given. The log of every load is
    kept, so a test can see all the choices made.
    """
    await page.goto(POST_URL)
    await page.set_content(html)
    for name in body_attrs:
        await page.evaluate("(n) => document.body.setAttribute(n, '')", name)
    session = PageSession(page)
    actions = PostSaveActions(session, PageNavigator(session))
    loads = 0
    LOG.clear()

    async def navigate(url: str) -> None:
        nonlocal loads
        loads += 1
        if loads == 1:
            return
        state = await page.evaluate(
            "() => [document.body.dataset.saved, document.body.dataset.log || '[]']"
        )
        LOG.extend(json.loads(state[1]))
        await page.set_content(reload_html or post_page(state[0] == "true"))
        for name in reload_attrs:
            await page.evaluate("(n) => document.body.setAttribute(n, '')", name)

    with (
        patch.object(actions._navigator, "_navigate_to_page", side_effect=navigate),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
    ):
        try:
            return await actions.set_post_saved(POST, saved)
        finally:
            LOG.extend(
                await page.evaluate(
                    "() => JSON.parse(document.body.dataset.log || '[]')"
                )
            )


async def test_saving_chooses_the_bookmark_item_of_the_posts_own_menu(dom_page):
    result = await act(dom_page, post_page(False), True)

    assert result == {
        "url": POST_URL,
        "post": f"/feed/update/{POST}/",
        "saved": True,
        "status": "saved",
    }
    # Opened, chose Save; then the reload's menu read the new state and closed.
    assert LOG == ["menu:post", "save", "menu:post", "escape"]


async def test_unsaving_a_saved_post(dom_page):
    result = await act(dom_page, post_page(True), False)

    assert result["status"] == "unsaved"
    assert LOG == ["menu:post", "save", "menu:post", "escape"]


async def test_a_saved_post_is_left_alone(dom_page):
    result = await act(dom_page, post_page(True), True)

    assert result["status"] == "already_saved"
    assert LOG == ["menu:post", "escape"]


async def test_an_unsaved_post_is_left_alone_by_unsave(dom_page):
    result = await act(dom_page, post_page(False), False)

    assert result["status"] == "already_unsaved"
    assert LOG == ["menu:post", "escape"]


async def test_a_menu_without_a_save_item_is_closed_with_nothing_chosen(dom_page):
    with pytest.raises(LinkedInOperationError, match="Nothing was chosen") as caught:
        await act(dom_page, post_page(False), True, body_attrs=["data-no-save"])
    assert type(caught.value) is LinkedInOperationError
    assert LOG == ["menu:post", "escape"]


async def test_a_post_without_its_menu_button_never_opens_a_comments(dom_page):
    with pytest.raises(LinkedInOperationError, match="Nothing was chosen"):
        await act(dom_page, post_page(False, menu=False), True)
    assert LOG == []


async def test_a_save_the_reload_does_not_show_is_an_unknown_outcome(dom_page):
    with pytest.raises(ActionOutcomeError, match="still showed"):
        await act(dom_page, post_page(False), True, body_attrs=["data-ignore"])


async def test_a_menu_unreadable_after_the_choice_is_an_unknown_outcome(dom_page):
    # The choice went through: saying nothing was chosen would be wrong.
    with pytest.raises(ActionOutcomeError) as caught:
        await act(
            dom_page, post_page(False), True, reload_html=post_page(False, menu=False)
        )
    assert "Nothing was chosen" not in str(caught.value)
