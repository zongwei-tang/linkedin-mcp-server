"""Browser-DOM tests for reading who reacted to a post.

``REACTORS_JS`` and ``SCROLL_JS`` never run under the unit suite's mocked
``page.evaluate``. These drive ``PostReactionsReader.read_post_reactions``
against a synthetic post page in headless chromium, with navigation stubbed.
The page carries the signals measured on 2026-11-22 and nothing else: the
reactors button marked by ``data-jump-link-target``, a dialog with one
``role="tab"`` per reaction (``data-js-reaction-tab``) showing its count, and
one row per reactor, a profile link beside an icon whose class carries the
reaction type, ten rows at first and ten more each time the dialog's own
scroll container reaches its end. Labels are German on purpose.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import post_reactions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_reactions import PostReactionsReader
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

POST = "urn:li:activity:7212345678901234567"
POST_URL = f"https://www.linkedin.com/feed/update/{POST}/"

# 25 reactors: every fifth celebrates, the rest like. Each link holds the name
# and, for screen readers only, a visually hidden "view profile" line. The dialog renders ten
# rows, then ten more each time its list is scrolled to the end.
SCRIPT = """(() => {
const total = 25;
const codeOf = (i) => (i % 5 === 4 ? 'PRAISE' : 'LIKE');
const row = (i) => '<li><div><a href="https://www.linkedin.com/in/person-' + i
    + '/?miniProfileUrn=x"><span aria-hidden="true">Person ' + i + '</span>'
    + '<span style="position:absolute;width:1px;height:1px;overflow:hidden;display:block">Profil von Person '
    + i + ' ansehen</span></a><img class="reactions-icon data-test-reactions-icon-type-'
    + codeOf(i) + '"></div><p>Beruf ' + i + '</p></li>';
const button = document.querySelector('[data-jump-link-target]');
button?.addEventListener('click', () => {
    const dialog = document.createElement('div');
    dialog.setAttribute('role', 'dialog');
    dialog.innerHTML = '<h2>Reaktionen</h2><div>'
        + '<button role="tab" data-js-reaction-tab="ALL">Alle 25</button>'
        + '<button role="tab" data-js-reaction-tab="LIKE"><img class="data-test-reactions-icon-type-LIKE">20</button>'
        + '<button role="tab" data-js-reaction-tab="PRAISE"><img class="data-test-reactions-icon-type-PRAISE">5</button>'
        + '</div><div class="list"><ul></ul></div>';
    document.body.appendChild(dialog);
    const list = dialog.querySelector('ul');
    let shown = 0;
    const more = () => {
        const until = Math.min(total, shown + 10);
        for (; shown < until; shown++) list.insertAdjacentHTML('beforeend', row(shown));
    };
    more();
    // Headed, LinkedIn pages with a button below the last row instead.
    if (document.body.hasAttribute('data-paged')) {
        const more_button = document.createElement('button');
        more_button.type = 'button';
        more_button.textContent = 'Weitere Ergebnisse anzeigen';
        more_button.addEventListener('click', () => {
            more();
            if (shown >= total) more_button.remove();
        });
        dialog.querySelector('.list').after(more_button);
    }
    const scroller = dialog.querySelector('.list');
    // As measured: the list only becomes a scroll box once it has rendered,
    // here 500 ms after its first rows when the body says data-late-box.
    const box = () => { scroller.style.height = '200px'; scroller.style.overflowY = 'auto'; };
    if (document.body.hasAttribute('data-late-box')) setTimeout(box, 500); else box();
    // A paged dialog loads only from its button, and closes from a button
    // below its list as well, which is never the one that loads more.
    if (document.body.hasAttribute('data-paged')) {
        dialog.insertAdjacentHTML('beforeend',
            '<button data-test-modal-close-btn aria-label="Schließen" '
            + 'onclick="this.parentElement.remove()">x</button>');
    }
    if (!document.body.hasAttribute('data-paged')) scroller.addEventListener('scroll', () => {
        if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 5) more();
    });
});
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') document.querySelector('[role="dialog"]')?.remove();
});
})();"""


def post_page(
    *, reactors: bool = True, late_box: bool = False, paged: bool = False
) -> str:
    button = (
        '<button aria-label="Weitere Reaktionen" '
        'data-jump-link-target="reactors-facepile-see-more-jump-target">+22</button>'
        if reactors
        else ""
    )
    return (
        "<body"
        + (" data-late-box" if late_box else "")
        + (" data-paged" if paged else "")
        + ">"
        + f'<main><div data-urn="{POST}"><p>Beitrag</p>{button}'
        '<button data-finite-scroll-hotkey="l"></button></div></main>'
        f"<script>{SCRIPT}</script>"
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
    monkeypatch.setattr(post_reactions, "_POST_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(post_reactions, "_DIALOG_TIMEOUT", 1.0)
    monkeypatch.setattr(post_reactions, "_SCROLL_WAIT", 1.5)
    monkeypatch.setattr(post_reactions, "_SETTLE", 0.0)


async def read(page, html: str, max_scrolls: int | None = None):
    await page.goto(POST_URL)
    await page.set_content(html)
    session = PageSession(page)
    reader = PostReactionsReader(session, PageNavigator(session))
    with (
        patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
    ):
        return await reader.read_post_reactions(POST, max_scrolls)


async def test_every_reactor_is_read_with_the_reaction_of_their_icon(dom_page):
    result = await read(dom_page, post_page())

    refs = result["references"]["reactions"]
    assert len(refs) == 25
    assert refs[0] == {
        "kind": "person",
        "url": "/in/person-0/",
        "text": "Person 0",
        "context": "like",
    }
    assert refs[4]["context"] == "celebrate"
    assert [r["context"] for r in refs].count("celebrate") == 5
    assert result["reaction_counts"] == {"total": 25, "like": 20, "celebrate": 5}
    assert result["url"] == POST_URL
    assert "Person 24" in result["sections"]["reactions"]


async def test_the_scrolls_bound_how_many_more_load(dom_page):
    # One scroll after the first ten: twenty people, not all twenty-five.
    result = await read(dom_page, post_page(), max_scrolls=1)

    assert len(result["references"]["reactions"]) == 20


async def test_the_dialog_is_closed_afterwards(dom_page):
    await read(dom_page, post_page())

    assert await dom_page.locator('[role="dialog"]').count() == 0


async def test_a_post_without_a_reactors_button_is_a_section_error(dom_page):
    result = await read(dom_page, post_page(reactors=False))

    assert result["sections"] == {}
    assert result["section_errors"]["reactions"]["error_type"] == "no_reactors_list"


async def test_a_list_that_becomes_scrollable_late_is_still_scrolled(dom_page):
    # The live failure: scrolled at once, it had nothing to scroll and only
    # the first ten were read.
    result = await read(dom_page, post_page(late_box=True))

    assert len(result["references"]["reactions"]) == 25


async def test_a_dialog_that_pages_with_a_button_is_paged_through(dom_page):
    # Measured headed: a "show more results" button below the last row, told
    # by its place and not its words, which are German here.
    result = await read(dom_page, post_page(paged=True))

    assert len(result["references"]["reactions"]) == 25
