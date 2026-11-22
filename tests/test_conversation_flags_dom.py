"""Browser-DOM tests for starring and muting a conversation.

The unit suite mocks ``page.evaluate``, so the programs behind
``ConversationFlagActions.set_thread_flag`` never execute there. These run the
whole action against a synthetic thread page in headless chromium, as the
archive tests do: the navigation is stubbed, and every stubbed load fetches
LinkedIn's conversations query, answered with the state the page holds (its
`STARRED` category and its `MUTE` or `ACTIVE` notification status, as measured
on 2026-11-22). The menu offers Star or Remove star and Mute or Unmute, opened
beside the thread's options button. None of LinkedIn's classes, so it is a
claim about which signals are read, not about LinkedIn's layout.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin import conversation_flag_actions
from linkedin_mcp_server.linkedin.conversation_flag_actions import (
    ConversationFlagActions,
    ThreadFlag,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import CONVERSATION_ACTIONS_EN_US as TEXT

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

THREAD_URL = "https://www.linkedin.com/messaging/thread/2-abc/"
CONVERSATIONS_URL = (
    "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"
    "?queryId=messengerConversations.test"
)

# The thread's menu says which way each item goes, from the body's state;
# choosing one flips that state unless the body says `data-refuse`. Every
# choice is recorded on the body, because patchright evaluates in an isolated
# world.
MENU_SCRIPT = (
    """(() => {
const chosen = [];
const record = (entry) => {
    chosen.push(entry);
    document.body.dataset.chosen = JSON.stringify(chosen);
};
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') document.querySelector('[role="menu"]')?.remove();
});
for (const button of document.querySelectorAll('main button[data-options]')) {
    button.addEventListener('click', () => {
        document.querySelector('[role="menu"]')?.remove();
        const body = document.body;
        const starred = body.hasAttribute('data-starred');
        const muted = body.hasAttribute('data-muted');
        const labels = body.hasAttribute('data-bare') ? ['Mark as unread', 'ARCHIVE']
            : ['Mark as unread', starred ? 'UNSTAR' : 'STAR',
               muted ? 'UNMUTE' : 'MUTE', 'ARCHIVE'];
        // An item whose words start with another's, as a menu could add.
        if (body.hasAttribute('data-extra')) labels.push('STAR messages');
        const menu = document.createElement('div');
        menu.setAttribute('role', 'menu');
        for (const label of labels) {
            const item = document.createElement('div');
            item.setAttribute('role', 'button');
            item.innerHTML = '<div><p>' + label + '</p></div>';
            item.addEventListener('click', () => {
                record([button.dataset.options, label]);
                menu.remove();
                if (button.dataset.options !== 'thread' || body.hasAttribute('data-refuse')) return;
                if (label === 'STAR' || label === 'UNSTAR') body.toggleAttribute('data-starred');
                if (label === 'MUTE' || label === 'UNMUTE') body.toggleAttribute('data-muted');
            });
            menu.appendChild(item);
        }
        button.parentElement.appendChild(menu);
    });
}
})();
""".replace("UNSTAR", TEXT.unstar_item)
    .replace("UNMUTE", TEXT.unmute_item)
    .replace("STAR", TEXT.star_item)
    .replace("MUTE", TEXT.mute_item)
    .replace("ARCHIVE", TEXT.archive_item)
)


def options(scope: str) -> str:
    return (
        f'<div><button data-options="{scope}"><span style="position:absolute;'
        f'width:1px;height:1px;overflow:hidden">{TEXT.options_prefix} Ada</span>'
        "…</button></div>"
    )


def thread_page(*, starred: bool = False, muted: bool = False, body: str = "") -> str:
    attributes = (" data-starred" if starred else "") + (" data-muted" if muted else "")
    return (
        f"<body{attributes}{body}><main>"
        '<ul><li><label aria-label="Select conversation with Ada"></label>'
        f"<div>Ada</div>{options('row')}</li></ul>"
        f"<section><h2>Ada</h2>{options('thread')}</section></main>"
        f"<script>{MENU_SCRIPT}</script></body>"
    )


@pytest.fixture
async def dom_page():
    async def answer(route):
        query = parse_qs(urlparse(route.request.url).query)
        if "state" in query:
            starred, muted = query["state"][0].split(",")
            conversation = {
                "_type": "com.linkedin.messenger.Conversation",
                "backendUrn": f"urn:li:messagingThread:{query['thread'][0]}",
                "categories": ["INBOX", "PRIMARY_INBOX"]
                + (["STARRED"] if starred == "1" else []),
                "notificationStatus": "MUTE" if muted == "1" else "ACTIVE",
            }
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"data": {"byIds": [conversation]}}),
            )
            return
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
    monkeypatch.setattr(conversation_flag_actions, "_THREAD_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(conversation_flag_actions, "_THREAD_MENU_TIMEOUT", 1.0)
    monkeypatch.setattr(conversation_flag_actions, "NAV_DELAY", 0.0)


async def act(page, html: str, flag: ThreadFlag, value: bool) -> dict[str, Any]:
    """Set one flag on the served page, each stubbed load fetching its state."""
    await page.goto(THREAD_URL)
    await page.set_content(html)
    session = PageSession(page)
    actions = ConversationFlagActions(session, PageNavigator(session))

    async def navigate(url: str) -> None:
        state = await page.evaluate(
            """() => [document.body.hasAttribute('data-silent'),
                document.body.hasAttribute('data-starred') ? '1' : '0',
                document.body.hasAttribute('data-muted') ? '1' : '0']"""
        )
        if state[0]:
            return
        thread = url.rstrip("/").rsplit("/", 1)[1]
        query = f"{CONVERSATIONS_URL}&thread={thread}&state={state[1]},{state[2]}"
        await page.evaluate("(u) => fetch(u).then((r) => r.text())", query)

    with patch.object(actions._navigator, "_navigate_to_page", side_effect=navigate):
        return await actions.set_thread_flag("2-abc", flag, value)


async def chosen(page) -> list[list[str]]:
    return await page.evaluate("() => JSON.parse(document.body.dataset.chosen || '[]')")


@pytest.mark.parametrize(
    ("flag", "value", "html", "word", "status"),
    [
        ("starred", True, thread_page(), TEXT.star_item, "starred"),
        ("starred", False, thread_page(starred=True), TEXT.unstar_item, "unstarred"),
        ("muted", True, thread_page(), TEXT.mute_item, "muted"),
        ("muted", False, thread_page(muted=True), TEXT.unmute_item, "unmuted"),
    ],
    ids=["star", "unstar", "mute", "unmute"],
)
async def test_the_threads_own_item_is_chosen_and_the_reload_confirms_it(
    dom_page, flag, value, html, word, status
):
    result = await act(dom_page, html, flag, value)

    assert result == {
        "url": THREAD_URL,
        "thread_id": "2-abc",
        flag: value,
        "status": status,
    }
    assert await chosen(dom_page) == [["thread", word]]


@pytest.mark.parametrize(
    ("flag", "value", "html", "status"),
    [
        ("starred", True, thread_page(starred=True), "already_starred"),
        ("muted", False, thread_page(), "already_unmuted"),
    ],
    ids=["starred", "unmuted"],
)
async def test_a_thread_already_as_asked_is_left_alone(
    dom_page, flag, value, html, status
):
    # Muting is told by the data, not by the menu, which is never opened.
    result = await act(dom_page, html, flag, value)

    assert result["status"] == status
    assert await chosen(dom_page) == []


async def test_star_never_takes_remove_star_or_another_rows_menu(dom_page):
    # "Star" is a prefix of nothing here, but the row's menu offers it too.
    result = await act(dom_page, thread_page(muted=True), "starred", True)

    assert result["status"] == "starred"
    assert await chosen(dom_page) == [["thread", TEXT.star_item]]


async def test_a_menu_without_the_item_chooses_nothing(dom_page):
    with pytest.raises(LinkedInOperationError, match="nothing was clicked") as caught:
        await act(dom_page, thread_page(body=" data-bare"), "starred", True)
    assert type(caught.value) is LinkedInOperationError
    assert await chosen(dom_page) == []


async def test_a_thread_whose_state_never_loads_is_not_touched(dom_page):
    with pytest.raises(LinkedInOperationError, match="nothing was clicked"):
        await act(dom_page, thread_page(body=" data-silent"), "muted", True)
    assert await chosen(dom_page) == []


async def test_a_choice_the_reload_does_not_show_is_an_unknown_outcome(dom_page):
    with pytest.raises(ActionOutcomeError, match="may not have kept it"):
        await act(dom_page, thread_page(body=" data-refuse"), "starred", True)
    assert await chosen(dom_page) == [["thread", TEXT.star_item]]


async def test_an_item_that_only_starts_with_the_words_is_never_taken(dom_page):
    # Whole text only: "Star messages" is not "Star".
    result = await act(dom_page, thread_page(body=" data-extra"), "starred", True)

    assert result["status"] == "starred"
    assert await chosen(dom_page) == [["thread", TEXT.star_item]]
