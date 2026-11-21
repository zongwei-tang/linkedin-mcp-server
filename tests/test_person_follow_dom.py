"""Browser-DOM tests for following and unfollowing a person.

``TOP_CARD_FOLLOW_JS`` and ``CONFIRM_UNFOLLOW_JS`` never run under the unit
suite's mocked ``page.evaluate``. These drive
``PersonActions.set_person_followed`` against a synthetic profile in headless
chromium, with navigation stubbed. The page behaves as measured on
2026-11-20: the top card's follow button around ``check-outline-small`` or
``plus-outline-small`` three levels below a More button (``aria-expanded``,
no ``aria-label``); a newsletter Subscribe button with the same plus icon
further from More; suggested profiles in ``<aside>``; and an unfollow that
asks first, adding a close button around an ``xmark`` icon, Cancel and
Unfollow. Labels are German on purpose.

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
from linkedin_mcp_server.linkedin import person_actions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.person_actions import PersonActions
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

URL = "https://www.linkedin.com/in/ada/"
CHECK = '<svg id="check-outline-small"></svg>'
PLUS = '<svg id="plus-outline-small"></svg>'

# Following is one click. Unfollowing opens a confirmation unless the page is
# told not to ask; Unfollow there flips the top card, Cancel closes it. Every
# click is logged by its data-who.
SCRIPT = """
const log = (what) => {
    const entries = JSON.parse(document.body.dataset.log || '[]');
    entries.push(what);
    document.body.dataset.log = JSON.stringify(entries);
};
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') log('escape');
});
const card = document.querySelector('[data-who="top"]');
const show = (followed) => {
    card.innerHTML = (followed ? '%s' : '%s') + (followed ? 'Folge ich' : 'Folgen');
};
const ask = () => {
    const dialog = document.createElement('div');
    dialog.setAttribute('role', 'dialog');
    dialog.innerHTML = '<button data-who="close"><svg id="xmark-outline-medium"></svg></button>'
        + '<h2>Ada entfolgen</h2><button data-who="cancel">Abbrechen</button>'
        + (document.body.hasAttribute('data-one-button') ? ''
           : '<button data-who="unfollow">Nicht mehr folgen</button>');
    dialog.addEventListener('click', (event) => {
        const who = event.target.closest('button')?.dataset.who;
        if (!who) return;
        log(who);
        if (who === 'unfollow' && !document.body.hasAttribute('data-ignore')) show(false);
        dialog.remove();
    });
    document.body.appendChild(dialog);
};
card.addEventListener('click', () => {
    log('top');
    const followed = !!card.querySelector('#check-outline-small');
    if (!followed) { if (!document.body.hasAttribute('data-ignore')) show(true); return; }
    if (document.body.hasAttribute('data-no-ask')) { show(false); return; }
    // A suggestion card in <aside>, rendered as the click lands, shaped
    // exactly like the confirmation: a close button around an xmark icon and
    // two plain buttons. Only being in <aside> tells it apart.
    if (document.body.hasAttribute('data-aside-card')) {
        document.querySelector('aside').insertAdjacentHTML('beforeend',
            '<div><button data-who="aside-close"><svg id="xmark-small"></svg></button>'
            + '<p>Grace</p><button data-who="aside-1">Nachricht</button>'
            + '<button data-who="aside-2">Vernetzen</button></div>');
    }
    if (document.body.hasAttribute('data-late')) { setTimeout(ask, 600); return; }
    // As live: the rest of the profile keeps rendering plain buttons.
    if (document.body.hasAttribute('data-busy-page')) {
        const section = document.createElement('section');
        for (let i = 0; i < 109; i++) section.appendChild(document.createElement('button'));
        // And a card that also holds two plain buttons beside an icon button
        // that is not an xmark, so it has no close button.
        section.insertAdjacentHTML('beforeend',
            '<div><button><svg id="close-small"></svg></button>'
            + '<button>Verbinden</button><button>Nachricht</button></div>');
        // An icon with an empty id and no test hook names nothing.
        section.insertAdjacentHTML('beforeend', '<button><svg id=""></svg></button>');
        document.querySelector('main').appendChild(section);
    }
    ask();
});
document.body.addEventListener('click', (event) => {
    const button = event.target.closest('aside button, [data-who="subscribe"]');
    if (button) log(button.dataset.who || 'aside');
});
""" % (CHECK, PLUS)


def profile(followed: bool, *, top_card: bool = True) -> str:
    icon = CHECK if followed else PLUS
    card = (
        "<section><div><div><div><div>"
        f'<button aria-label="Ada folgen" data-who="top">{icon}Folgen</button>'
        "</div></div>"
        '<button aria-expanded="false">Mehr</button>'
        "</div></div></section>"
        if top_card
        else "<section><p>Ada</p></section>"
    )
    return (
        "<main>"
        f"{card}"
        "<section><div><div><div><div><div>"
        f'<button aria-label="Newsletter abonnieren" data-who="subscribe">{PLUS}Abonnieren</button>'
        "</div></div></div></div>"
        '<button aria-expanded="false">Mehr</button></div></section>'
        '<aside><div><button aria-label="Grace folgen">'
        f'{PLUS}Folgen</button><button aria-expanded="false"></button></div></aside>'
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
    monkeypatch.setattr(person_actions, "_PROFILE_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(person_actions, "_FOLLOW_TIMEOUT", 1.0)
    monkeypatch.setattr(person_actions, "NAV_DELAY", 0.0)


async def act(page, html: str, followed: bool, *, body_attrs=(), reload_html=None):
    await page.goto(URL)
    await page.set_content(html)
    for name in body_attrs:
        await page.evaluate("(n) => document.body.setAttribute(n, '')", name)
    session = PageSession(page)
    actions = PersonActions(session, PageNavigator(session))
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
        return await actions.set_person_followed("ada", followed)


async def log(page) -> list[str]:
    return await page.evaluate("() => JSON.parse(document.body.dataset.log || '[]')")


async def test_following_is_one_click_on_the_top_card(dom_page):
    result = await act(dom_page, profile(False), True)

    assert result == {
        "url": URL,
        "username": "ada",
        "followed": True,
        "status": "followed",
    }
    assert await log(dom_page) == ["top"]


async def test_unfollowing_confirms_with_the_second_plain_button(dom_page):
    result = await act(dom_page, profile(True), False)

    assert result["status"] == "unfollowed"
    assert await log(dom_page) == ["top", "unfollow"]


async def test_an_unfollow_that_does_not_ask_is_still_confirmed(dom_page):
    result = await act(dom_page, profile(True), False, body_attrs=["data-no-ask"])

    assert result["status"] == "unfollowed"
    assert await log(dom_page) == ["top"]


async def test_a_person_already_followed_is_left_alone(dom_page):
    result = await act(dom_page, profile(True), True)

    assert result["status"] == "already_followed"
    assert await log(dom_page) == []


async def test_a_person_not_followed_is_left_alone_by_unfollow(dom_page):
    # The newsletter and the suggestions carry the plus icon too.
    result = await act(dom_page, profile(False), False)

    assert result["status"] == "already_unfollowed"
    assert await log(dom_page) == []


async def test_a_top_card_without_follow_never_clicks_a_suggestion(dom_page):
    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, profile(False, top_card=False), True)
    assert await log(dom_page) == []


async def test_a_confirmation_of_another_shape_is_closed_unconfirmed(dom_page):
    with pytest.raises(LinkedInOperationError, match="nothing was confirmed"):
        await act(dom_page, profile(True), False, body_attrs=["data-one-button"])
    assert "unfollow" not in await log(dom_page)


async def test_a_click_that_changes_nothing_is_an_error(dom_page):
    # Clicked, so the account may have changed: an unknown outcome, and
    # whatever the click opened is closed.
    with pytest.raises(ActionOutcomeError, match="did not change"):
        await act(dom_page, profile(False), True, body_attrs=["data-ignore"])
    assert await log(dom_page) == ["top", "escape"]


async def test_a_follow_the_reload_does_not_show_is_an_error(dom_page):
    with pytest.raises(ActionOutcomeError, match="not after a reload"):
        await act(dom_page, profile(False), True, reload_html=profile(False))


TWO_TOP_CARD_BUTTONS = (
    '<div><div><button data-who="top2"><svg id="plus-outline-small"></svg>'
    'Folgen</button></div><button aria-expanded="false">Mehr</button></div>'
)


@pytest.mark.parametrize(
    "reload_html",
    [
        profile(False, top_card=False),
        profile(False).replace(
            "</section><section>", f"{TWO_TOP_CARD_BUTTONS}</section><section>", 1
        ),
    ],
    ids=["no-top-card", "two-follow-buttons"],
)
async def test_a_top_card_unreadable_after_the_click_is_an_unknown_outcome(
    dom_page, reload_html
):
    # The click went through: saying nothing was clicked would be wrong.
    with pytest.raises(ActionOutcomeError) as caught:
        await act(dom_page, profile(False), True, reload_html=reload_html)
    assert "Nothing was clicked" not in str(caught.value)


async def test_buttons_rendered_elsewhere_while_it_asks_are_ignored(dom_page):
    # The live failure: 109 new plain buttons beside the confirmation's two.
    result = await act(dom_page, profile(True), False, body_attrs=["data-busy-page"])

    assert result["status"] == "unfollowed"
    assert await log(dom_page) == ["top", "unfollow"]


async def test_a_late_confirmation_is_waited_for_and_an_aside_card_never_clicked(
    dom_page,
):
    # Review finding: a suggestion card shaped like the confirmation appears at
    # once, and the confirmation itself only after a poll.
    result = await act(
        dom_page, profile(True), False, body_attrs=["data-aside-card", "data-late"]
    )

    assert result["status"] == "unfollowed"
    assert await log(dom_page) == ["top", "unfollow"]


async def test_two_top_card_follow_buttons_are_not_guessed_between(dom_page):
    html = profile(False).replace(
        "</section><section>", f"{TWO_TOP_CARD_BUTTONS}</section><section>", 1
    )
    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, True)
    assert await log(dom_page) == []
