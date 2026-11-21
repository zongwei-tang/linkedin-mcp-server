"""Browser-DOM tests for choosing a reaction on a post.

``REACTION_MENU_PREPARE_JS``, ``REACTION_CHOICE_JS`` and the type-code read in
``POST_LIKE_JS`` never run under the unit suite's mocked ``page.evaluate``.
These drive ``PostActions.set_post_reaction`` against a synthetic post in
headless chromium, with navigation stubbed. The page behaves as measured on
2026-11-20: the post-level ``data-finite-scroll-hotkey="l"`` caret adds six
unlabelled-by-icon buttons in the order Like, Celebrate, Support, Love,
Insightful, Funny; choosing one presses the post's button and puts in it an
image whose class carries ``data-test-reactions-icon-type-<CODE>``. Labels
are German on purpose.

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
CODES = ["LIKE", "PRAISE", "APPRECIATION", "EMPATHY", "INTEREST", "ENTERTAINMENT"]
LABELS = ["Gefällt mir", "Applaus", "Unterstützung", "Liebe", "Wissenswert", "Lustig"]

# The caret adds the menu at the end of the body; a choice records its code,
# closes the menu and shows the reaction on the post's button, unless the page
# is told to show another one. Clicking the pressed button takes it back.
SCRIPT = """
const LABELS = %s;
const CODES = %s;
const main = document.querySelector('[data-who="post"]');
const show = (code) => {
    main.setAttribute('aria-pressed', code ? 'true' : 'false');
    main.innerHTML = code
        ? '<img class="reactions-icon data-test-reactions-icon-type-' + code + '">'
        : '<svg data-test-icon="thumbs-up-outline-small"></svg>';
};
const record = (what) => {
    const log = JSON.parse(document.body.dataset.log || '[]');
    log.push(what);
    document.body.dataset.log = JSON.stringify(log);
};
main.addEventListener('click', () => {
    record('main');
    show(main.getAttribute('aria-pressed') === 'true' ? null : 'LIKE');
});
document.querySelector('[data-finite-scroll-hotkey="l"]').addEventListener('click', () => {
    record('caret');
    if (document.body.hasAttribute('data-short-menu')) {
        document.body.insertAdjacentHTML('beforeend', '<button>Gefällt mir</button>');
        return;
    }
    // As live: other parts of the page render buttons while the menu opens.
    if (document.body.hasAttribute('data-busy-page')) {
        for (let i = 0; i < 6; i++) {
            document.body.insertAdjacentHTML('beforeend', '<div><button>Antworten</button></div>');
        }
    }
    if (document.body.hasAttribute('data-two-menus')) {
        const other = document.createElement('div');
        for (let i = 0; i < 6; i++) other.appendChild(document.createElement('button'));
        document.body.appendChild(other);
    }
    const menu = document.createElement('div');
    // As live: the menu is not visible until a hover animates it in.
    if (document.body.hasAttribute('data-hidden-menu')) {
        menu.style.visibility = 'hidden';
    }
    LABELS.forEach((label, index) => {
        const button = document.createElement('button');
        button.setAttribute('aria-label', 'Reagieren: ' + label);
        button.addEventListener('click', () => {
            record(CODES[index]);
            menu.remove();
            show(document.body.dataset.force || CODES[index]);
        });
        menu.appendChild(button);
    });
    document.body.appendChild(menu);
});
""" % (LABELS, CODES)


def post_page(code: str | None = None) -> str:
    inner = (
        f'<img class="reactions-icon data-test-reactions-icon-type-{code}">'
        if code
        else '<svg data-test-icon="thumbs-up-outline-small"></svg>'
    )
    pressed = "true" if code else "false"
    return (
        f'<main><div data-urn="{POST}"><p>Beitrag</p><div>'
        f'<span><button aria-pressed="{pressed}" data-who="post">{inner}</button></span>'
        '<span><button aria-expanded="false" data-finite-scroll-hotkey="l"></button></span>'
        "</div></div>"
        '<article data-id="urn:li:comment:1"><div><span>'
        '<button aria-pressed="false">Gefällt mir</button></span>'
        '<span><button data-finite-scroll-hotkey="l"></button></span></div></article>'
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
    monkeypatch.setattr(post_actions, "_POST_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(post_actions, "_LIKE_TIMEOUT", 1.0)
    monkeypatch.setattr(post_actions, "_MENU_TIMEOUT", 1.0)
    monkeypatch.setattr(post_actions, "NAV_DELAY", 0.0)


async def act(page, html: str, reaction: str, *, body_attrs: dict | None = None):
    await page.goto(POST_URL)
    await page.set_content(html)
    for name, value in (body_attrs or {}).items():
        await page.evaluate(
            "([n, v]) => document.body.setAttribute(n, v)", [name, value]
        )
    session = PageSession(page)
    actions = PostActions(session, PageNavigator(session))
    with (
        patch.object(actions._navigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
    ):
        # A plain string on purpose: the unknown-reaction test passes one.
        return await actions.set_post_reaction(POST, reaction)  # ty: ignore[invalid-argument-type]


async def log(page) -> list[str]:
    return await page.evaluate("() => JSON.parse(document.body.dataset.log || '[]')")


@pytest.mark.parametrize(("reaction", "code"), list(zip(post_actions.REACTIONS, CODES)))
async def test_each_reaction_is_chosen_by_its_place_in_the_menu(
    dom_page, reaction, code
):
    # From an existing different reaction, so even Like goes through the menu.
    start = "ENTERTAINMENT" if code != "ENTERTAINMENT" else "PRAISE"
    result = await act(dom_page, post_page(start), reaction)

    assert result == {
        "url": POST_URL,
        "post": f"/feed/update/{POST}/",
        "reaction": reaction,
        "status": "reacted",
    }
    assert await log(dom_page) == ["caret", code]


async def test_like_on_an_unreacted_post_is_the_button_itself(dom_page):
    result = await act(dom_page, post_page(None), "like")

    assert result["status"] == "reacted"
    assert await log(dom_page) == ["main"]


async def test_celebrate_on_an_unreacted_post_goes_through_the_menu(dom_page):
    result = await act(dom_page, post_page(None), "celebrate")

    assert result["status"] == "reacted"
    assert await log(dom_page) == ["caret", "PRAISE"]


async def test_the_reaction_already_shown_is_left_alone(dom_page):
    result = await act(dom_page, post_page("EMPATHY"), "love")

    assert result["status"] == "already_reacted"
    assert await log(dom_page) == []


async def test_a_pressed_button_without_a_code_counts_as_like(dom_page):
    html = post_page(None).replace(
        'aria-pressed="false" data-who="post"', 'aria-pressed="true" data-who="post"'
    )

    result = await act(dom_page, html, "like")

    assert result["status"] == "already_reacted"
    assert await log(dom_page) == []


async def test_a_reaction_other_than_the_one_chosen_is_an_error(dom_page):
    with pytest.raises(LinkedInOperationError, match="reaction type ENTERTAINMENT"):
        await act(
            dom_page,
            post_page(None),
            "support",
            body_attrs={"data-force": "ENTERTAINMENT"},
        )


async def test_a_menu_without_six_new_buttons_chooses_nothing(dom_page):
    with pytest.raises(LinkedInOperationError, match="Nothing was chosen"):
        await act(
            dom_page, post_page(None), "insightful", body_attrs={"data-short-menu": ""}
        )
    assert await log(dom_page) == ["caret"]


async def test_an_unknown_reaction_never_opens_the_post(dom_page):
    with pytest.raises(ValueError, match="reaction must be one of"):
        await act(dom_page, post_page(None), "angry")
    assert await log(dom_page) == []


async def test_a_menu_hidden_until_hovered_is_still_chosen_from(dom_page):
    # The live failure: a real click waits for the menu to become visible and
    # never sees it. The caret is hidden the same way.
    html = post_page(None).replace(
        'data-finite-scroll-hotkey="l"></button></span></div></div>',
        'data-finite-scroll-hotkey="l" style="visibility: hidden"></button></span></div></div>',
        1,
    )
    result = await act(dom_page, html, "celebrate", body_attrs={"data-hidden-menu": ""})

    assert result["status"] == "reacted"
    assert await log(dom_page) == ["caret", "PRAISE"]


async def test_buttons_rendered_elsewhere_while_the_menu_opens_are_ignored(dom_page):
    # The second live failure: 12 new buttons, six of them not the menu's.
    result = await act(
        dom_page, post_page("PRAISE"), "love", body_attrs={"data-busy-page": ""}
    )

    assert result["status"] == "reacted"
    assert await log(dom_page) == ["caret", "EMPATHY"]


async def test_two_groups_of_six_choose_nothing(dom_page):
    with pytest.raises(LinkedInOperationError, match="Nothing was chosen"):
        await act(dom_page, post_page(None), "love", body_attrs={"data-two-menus": ""})
    assert await log(dom_page) == ["caret"]
