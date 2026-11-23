"""Browser-DOM tests for muting and unmuting a person.

``ACTIVITY_JS``, ``HIDE_ITEM_JS`` and ``UNFOLLOWED_JS`` never run under the
unit suite's mocked ``page.evaluate``. These drive ``MuteActions`` against two
synthetic pages in headless chromium, with navigation stubbed: the person's
activity page and Settings' list of people unfollowed or muted. They carry the
signals measured on 2026-11-23 and nothing else: a header More button with
the ellipsis icon ahead of the author's link (path ``/in/<username>/``, their
name its first line), each post's control menu button, labelled, around the
ellipsis icon and without aria-expanded, opening ``[role="menu"]`` items
around ``eye-slash-outline-medium`` (hide) or ``circle-xmark-outline-medium``
(unfollow); and the list's ``[data-test-unfollowed-card]`` with its stated
count, one ``li`` per person named by ``[data-test-profile-card-name]``,
beside ``button[data-test-unfollowed-member__follow-btn]``. Labels are German
on purpose.

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
from linkedin_mcp_server.linkedin import mute_actions
from linkedin_mcp_server.linkedin.mute_actions import UNFOLLOWED_URL, MuteActions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

USER = "ada-lovelace"
NAME = "Ada L."

SCRIPT = """(() => {
const log = (what) => {
    const entries = JSON.parse(document.body.dataset.log || '[]');
    entries.push(what);
    document.body.dataset.log = JSON.stringify(entries);
};
const item = (icon, words, what) => '<div role="menuitem" data-what="' + what + '"><svg id="'
    + icon + '"></svg><span>' + words + '</span></div>';
for (const button of document.querySelectorAll('button[data-who]')) {
    button.addEventListener('click', () => {
        log('menu:' + button.dataset.who);
        const menu = document.createElement('div');
        menu.setAttribute('role', 'menu');
        const followed = document.body.hasAttribute('data-followed');
        menu.innerHTML = item('bookmark-outline-medium', 'Speichern', 'save')
            + (followed ? item('circle-xmark-outline-medium', 'Nicht mehr folgen', 'unfollow')
                        : item('eye-slash-outline-medium', 'Beiträge ausblenden', 'hide'))
            + item('flag-outline-medium', 'Melden', 'report');
        menu.addEventListener('click', (event) => {
            const what = event.target.closest('[role="menuitem"]')?.dataset.what;
            if (!what) return;
            log(what);
            if (what === 'hide' && !document.body.hasAttribute('data-ignore')) {
                document.body.dataset.change = 'mute';
            }
            menu.remove();
        });
        document.body.appendChild(menu);
    });
}
for (const button of document.querySelectorAll('button[data-test-unfollowed-member__follow-btn]')) {
    button.addEventListener('click', () => {
        const name = button.closest('li').querySelector('[data-test-profile-card-name]').innerText;
        log('follow:' + name);
        if (!document.body.hasAttribute('data-ignore')) document.body.dataset.change = 'unmute:' + name;
    });
}
document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    log('escape');
    for (const m of document.querySelectorAll('[role="menu"]')) m.remove();
});
})();"""


def post(who: str) -> str:
    return (
        f"<div><h2>Beitrag</h2><button aria-label='Steuerungsmenü für Beitrag' data-who='{who}'>"
        "<svg id='ellipses-horizontal-fill-small'></svg></button><p>Text</p></div>"
    )


def activity_page(
    *, posts: int = 2, named: bool = True, inline_degree: bool = False
) -> str:
    author = (
        f"<a href='https://www.linkedin.com/in/{USER}?miniProfileUrn=x'>"
        + (
            f"<span>{NAME} • 3.+</span>"
            if inline_degree
            else f"<div>{NAME}</div><div> • 3.+</div>"
        )
        + "</a>"
        if named
        else ""
    )
    return (
        "<main><div><button aria-label='Mehr' data-who='header'>"
        "<svg id='ellipses-horizontal-fill-small'></svg></button>"
        f"<a href='https://www.linkedin.com/in/{USER}/recent-activity/all/'>Beiträge</a></div>"
        f"<div>{author}{''.join(post(f'post{i}') for i in range(posts))}</div></main>"
        f"<script>{SCRIPT}</script>"
    )


def list_page(names: list[str], *, stated: int | None = None) -> str:
    rows = "".join(
        f"<li><div><div data-test-profile-card-name>{n}</div></div>"
        f"<button data-test-unfollowed-member__follow-btn='{i}' type='button'>"
        f"<span><span aria-label='{n} folgen'>Folgen</span></span></button></li>"
        for i, n in enumerate(names)
    )
    count = len(names) if stated is None else stated
    return (
        "<main><div data-test-unfollowed-card>"
        f"<p data-test-unfollowed-card__primary-desc>Sie haben {count} Personen entfolgt</p>"
        f"<ul>{rows}</ul></div></main><script>{SCRIPT}</script>"
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
    monkeypatch.setattr(mute_actions, "_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(mute_actions, "_MENU_TIMEOUT", 1.0)
    monkeypatch.setattr(mute_actions, "_LIST_SETTLE", 0.0)
    monkeypatch.setattr(mute_actions, "NAV_DELAY", 0.0)


class Site:
    """The list of people unfollowed or muted, kept between loads."""

    def __init__(
        self, page, listed, *, activity=None, body_attrs=(), late=0, stated=None
    ):
        self.page = page
        self.listed = list(listed)
        self.activity = activity or activity_page()
        self.body_attrs = body_attrs
        #: How many list loads still show an unmute that LinkedIn has taken.
        self.late = late
        self.pending: str | None = None
        self.stated = stated
        self.urls: list[str] = []
        self.log: list[str] = []

    async def collect(self) -> None:
        state = await self.page.evaluate(
            "() => { const b = document.body; if (!b) return ['[]', ''];"
            " const out = [b.dataset.log || '[]', b.dataset.change || ''];"
            " delete b.dataset.log; delete b.dataset.change; return out; }"
        )
        self.log.extend(json.loads(state[0]))
        if state[1] == "mute":
            self.listed.append(NAME)
        elif state[1].startswith("unmute:"):
            self.pending = state[1].split(":", 1)[1]

    async def navigate(self, url: str) -> None:
        await self.collect()
        self.urls.append(url)
        if url == UNFOLLOWED_URL:
            if self.pending:
                if self.late:
                    self.late -= 1
                else:
                    self.listed.remove(self.pending)
                    self.pending = None
            content = list_page(self.listed, stated=self.stated)
        else:
            content = self.activity
        await self.page.set_content(content)
        for name in self.body_attrs:
            await self.page.evaluate("(n) => document.body.setAttribute(n, '')", name)


async def run(page, site: Site, muted: bool):
    await page.goto("https://www.linkedin.com/feed/")
    session = PageSession(page)
    actions = MuteActions(session, PageNavigator(session))
    with (
        patch.object(
            actions._navigator, "_navigate_to_page", side_effect=site.navigate
        ),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
    ):
        try:
            return await actions.set_person_muted(USER, muted)
        finally:
            await site.collect()


async def test_muting_hides_the_first_posts_author_not_the_header(dom_page):
    site = Site(dom_page, ["Grace H."])

    result = await run(dom_page, site, True)

    assert result == {
        "url": f"https://www.linkedin.com/in/{USER}/",
        "username": USER,
        "name": NAME,
        "muted": True,
        "status": "muted",
    }
    assert site.log == ["menu:post0", "hide"]
    assert site.listed == ["Grace H.", NAME]
    assert site.urls[0].endswith(f"/in/{USER}/recent-activity/all/")


async def test_a_degree_on_the_names_line_is_not_part_of_the_name(dom_page):
    site = Site(dom_page, [NAME], activity=activity_page(inline_degree=True))

    result = await run(dom_page, site, True)

    assert result["name"] == NAME
    assert result["status"] == "already_muted"


async def test_someone_already_listed_is_already_muted(dom_page):
    site = Site(dom_page, ["Grace H.", NAME])

    result = await run(dom_page, site, True)

    assert result["status"] == "already_muted"
    assert site.log == []


async def test_someone_followed_is_pointed_to_unfollow_with_nothing_chosen(dom_page):
    site = Site(dom_page, [], body_attrs=["data-followed"])

    with pytest.raises(LinkedInOperationError, match="unfollow_person") as caught:
        await run(dom_page, site, True)
    assert type(caught.value) is LinkedInOperationError
    assert site.log == ["menu:post0", "escape"]
    assert site.listed == []


async def test_a_person_without_posts_cannot_be_muted(dom_page):
    site = Site(dom_page, [], activity=activity_page(posts=0))

    with pytest.raises(LinkedInOperationError, match="no post with a menu"):
        await run(dom_page, site, True)
    assert site.log == []


async def test_an_activity_page_without_their_name_changes_nothing(dom_page):
    site = Site(dom_page, [], activity=activity_page(named=False))

    with pytest.raises(LinkedInOperationError, match="did not show their name"):
        await run(dom_page, site, True)
    assert site.urls == [f"https://www.linkedin.com/in/{USER}/recent-activity/all/"]


async def test_a_mute_linkedin_does_not_keep_is_an_unknown_outcome(dom_page):
    site = Site(dom_page, [], body_attrs=["data-ignore"])

    with pytest.raises(ActionOutcomeError, match="does not list"):
        await run(dom_page, site, True)


async def test_unmuting_clicks_the_named_persons_follow_button(dom_page):
    site = Site(dom_page, ["Grace H.", NAME, "Alan T."])

    result = await run(dom_page, site, False)

    assert result["status"] == "unmuted"
    assert site.log == [f"follow:{NAME}"]
    assert site.listed == ["Grace H.", "Alan T."]


async def test_a_removal_that_lands_late_is_still_confirmed(dom_page):
    # Measured live: right after the click the list still showed the person.
    site = Site(dom_page, [NAME], late=1)

    result = await run(dom_page, site, False)

    assert result["status"] == "unmuted"


async def test_someone_not_listed_is_already_unmuted(dom_page):
    site = Site(dom_page, ["Grace H."])

    result = await run(dom_page, site, False)

    assert result["status"] == "already_unmuted"
    assert site.log == []


async def test_two_people_of_that_name_are_never_guessed_between(dom_page):
    site = Site(dom_page, [NAME, NAME])

    with pytest.raises(LinkedInOperationError, match="2 people named"):
        await run(dom_page, site, False)
    assert site.log == []


async def test_a_list_showing_fewer_than_it_states_is_not_trusted(dom_page):
    site = Site(dom_page, ["Grace H."], stated=40)

    with pytest.raises(LinkedInOperationError, match="states 40 people"):
        await run(dom_page, site, False)
    assert site.log == []
