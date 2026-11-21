"""Browser-DOM tests for liking and unliking a post.

The unit suite mocks ``page.evaluate``, so ``POST_LIKE_JS`` never executes
there. These run ``PostActions.set_post_liked`` against a synthetic post page
in headless chromium, with navigation stubbed. The markup carries the signals
measured on a `/feed/update/<urn>/` page on 2026-11-20 and nothing else: the
post inside an element with `data-urn`, its reactions caret
`button[data-finite-scroll-hotkey="l"]` beside a `button[aria-pressed]` Like,
and comments as `article[data-id]` with their own caret and Like. The labels
are German on purpose: none of them may matter.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin import post_actions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_actions import PostActions
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

POST = "urn:li:activity:7212345678901234567"
POST_URL = f"https://www.linkedin.com/feed/update/{POST}/"

# A click on a Like flips its aria-pressed and records which one was pressed,
# unless the page is told to ignore clicks. Recorded on the body, because
# patchright evaluates in an isolated world.
LIKE_SCRIPT = """
for (const button of document.querySelectorAll('button[aria-pressed]')) {
    button.addEventListener('click', () => {
        const pressed = JSON.parse(document.body.dataset.pressed || '[]');
        pressed.push(button.dataset.who);
        document.body.dataset.pressed = JSON.stringify(pressed);
        if (document.body.hasAttribute('data-ignore')) return;
        const now = button.getAttribute('aria-pressed') !== 'true';
        button.setAttribute('aria-pressed', String(now));
    });
}
"""


def action_bar(pressed: bool, who: str) -> str:
    return (
        "<div>"
        f'<span><button aria-pressed="{str(pressed).lower()}" '
        f'aria-label="Reagieren: Gefällt mir" data-who="{who}">'
        '<svg data-test-icon="thumbs-up-outline-small"></svg>Gefällt mir</button></span>'
        '<span><button aria-expanded="false" data-finite-scroll-hotkey="l" '
        'aria-label="Reaktionsmenü öffnen"></button></span>'
        '<div><span><button data-finite-scroll-hotkey="c">Kommentieren</button></span></div>'
        '<span><button data-finite-scroll-hotkey="r" aria-expanded="false">'
        "Teilen</button></span>"
        "</div>"
    )


def comment(pressed: bool, who: str) -> str:
    return (
        f'<article data-id="urn:li:comment:{who}"><p>Kommentar</p>'
        f'<div><span><button aria-pressed="{str(pressed).lower()}" data-who="{who}">'
        "Gefällt mir</button></span>"
        '<span><button data-finite-scroll-hotkey="l" aria-expanded="false"></button>'
        "</span></div></article>"
    )


def post_page(post_pressed: bool = False, *, comments: str | None = None) -> str:
    comments = (
        comments if comments is not None else comment(True, "c1") + comment(False, "c2")
    )
    return (
        f'<main><div role="article" data-urn="{POST}">'
        '<button aria-pressed="false" data-who="follow">'
        '<svg data-test-icon="add-small"></svg>Folgen</button>'
        f"<p>Beitrag</p>{action_bar(post_pressed, 'post')}</div>"
        f"<section>{comments}</section></main><script>{LIKE_SCRIPT}</script>"
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
    monkeypatch.setattr(post_actions, "_POST_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(post_actions, "_LIKE_TIMEOUT", 1.0)
    monkeypatch.setattr(post_actions, "NAV_DELAY", 0.0)


async def act(page, html: str, liked: bool, *, reload_html: str | None = None):
    """Serve ``html`` as the post, and ``reload_html`` as its next load."""
    await page.goto(POST_URL)
    await page.set_content(html)
    session = PageSession(page)
    actions = PostActions(session, PageNavigator(session))
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
        return await actions.set_post_liked(POST, liked)


async def pressed(page) -> list[str]:
    return await page.evaluate(
        "() => JSON.parse(document.body.dataset.pressed || '[]')"
    )


async def test_a_like_the_reload_shows_is_reported_liked(dom_page):
    result = await act(dom_page, post_page(False), True, reload_html=post_page(True))

    assert result == {
        "url": POST_URL,
        "post": f"/feed/update/{POST}/",
        "liked": True,
        "status": "liked",
    }


async def test_a_like_the_reload_does_not_show_is_an_error(dom_page):
    with pytest.raises(LinkedInOperationError, match="not after a reload"):
        await act(dom_page, post_page(False), True, reload_html=post_page(False))


async def test_a_liked_post_is_left_alone(dom_page):
    result = await act(dom_page, post_page(True), True)

    assert result["status"] == "already_liked"
    assert await pressed(dom_page) == []


async def test_unliking_presses_the_liked_post_once(dom_page):
    result = await act(dom_page, post_page(True), False)

    assert result["status"] == "unliked"
    assert await pressed(dom_page) == ["post"]


async def test_liking_presses_only_the_post(dom_page):
    result = await act(dom_page, post_page(False), True)

    assert result["status"] == "liked"
    assert await pressed(dom_page) == ["post"]


async def test_an_already_unliked_post_with_liked_comments_is_left_alone(dom_page):
    # The first comment's Like is pressed; the post's is not.
    result = await act(dom_page, post_page(False), False)

    assert result["status"] == "already_unliked"
    assert await pressed(dom_page) == []


async def test_a_click_the_page_ignores_is_an_error(dom_page):
    await dom_page.goto(POST_URL)
    await dom_page.set_content(post_page(False))
    await dom_page.evaluate("() => document.body.setAttribute('data-ignore', '')")
    session = PageSession(dom_page)
    actions = PostActions(session, PageNavigator(session))
    with (
        patch.object(actions._navigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
        pytest.raises(LinkedInOperationError, match="did not change"),
    ):
        await actions.set_post_liked(POST, True)
    assert await pressed(dom_page) == ["post"]


async def test_a_page_without_one_post_caret_is_not_clicked(dom_page):
    # Two post-level carets: the page is not the measured shape.
    html = post_page(False).replace(
        "<section>", f"<section>{action_bar(False, 'other')}", 1
    )
    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, True)
    assert await pressed(dom_page) == []


async def test_a_post_with_only_comment_controls_is_not_clicked(dom_page):
    html = (
        f'<main><div data-urn="{POST}"><p>Beitrag</p></div>'
        f"<section>{comment(False, 'c1')}</section></main>"
        f"<script>{LIKE_SCRIPT}</script>"
    )
    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, True)
    assert await pressed(dom_page) == []


async def test_a_comment_beside_the_caret_never_stands_in_for_the_posts_like(dom_page):
    # The post's Like did not render, and a comment preview sits in the same
    # row as its caret: the comment's Like is close enough to be found by the
    # walk up from the caret, and must not be taken for the post's.
    html = (
        f'<main><div data-urn="{POST}"><div>'
        '<span><button data-finite-scroll-hotkey="l" aria-expanded="false">'
        f"</button></span>{comment(False, 'c1')}</div></div></main>"
        f"<script>{LIKE_SCRIPT}</script>"
    )
    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, True)
    assert await pressed(dom_page) == []
