"""Mute and unmute a person: hide their posts from the feed, and undo it.

Measured 2026-11-23, English locale, by muting one person the account did not
follow from their post's menu and unmuting them again:

- The control menu of a post by someone the account does not follow (a
  `button[aria-label]` around an `ellipses-*` icon, opening `[role="menu"]`)
  offers an
  item around `eye-slash-outline-medium`: "Hide posts by <name>". LinkedIn
  answers "You'll no longer see posts from <name> in your feed. You can undo
  this in Settings". For someone the account follows, the same place offers
  Unfollow (around `circle-xmark-outline-medium`) instead, which is
  unfollow_person's.
- The person's activity page (`/in/<username>/recent-activity/all/`) lists
  their posts with the same menus. The author's name there is the first line
  of the link to their profile (`a[href]` whose path is `/in/<username>/`),
  their degree (" • 3rd+") on the line after it.
- Settings' "People you unfollowed" page (`/mypreferences/m/unfollowed/`)
  lists everyone unfollowed or muted, one `li` each in
  `[data-test-unfollowed-card]`, named by `[data-test-profile-card-name]` and
  without a link to the profile, beside its
  `button[data-test-unfollowed-member__follow-btn]`. That button is LinkedIn's
  only way back: it takes the person off the list, and follows them
  ("Followed <name> successfully").

So the menu item is found by its icon id, the state is read from the settings
list, and the person is matched there by the name their own activity page
shows, the one identifier that list carries.

Verified live 2026-11-23 on a stranger's posts: mute, then unmute.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

MuteStatus = Literal["muted", "unmuted", "already_muted", "already_unmuted"]

UNFOLLOWED_URL = "https://www.linkedin.com/mypreferences/m/unfollowed/"
_MENU_MARK = "data-mcp-mute-menu"
_ITEM_MARK = "data-mcp-mute-item"
_FOLLOW_MARK = "data-mcp-unmute"
_READY_TIMEOUT = 12.0
_MENU_TIMEOUT = 5.0
_LIST_SCROLLS = 5
#: The list is read up to this many times after a change, _LIST_SETTLE apart:
#: measured live 2026-11-23, an unmute left the person listed for a moment.
_LIST_READS = 3
_LIST_SETTLE = 4.0
_POLL = 0.25

# The activity page's author name and its first post's control menu button,
# marked: the first labelled button around the ellipsis icon after the first
# link to the author (measured: it carries no aria-expanded until opened).
ACTIVITY_JS = r"""([user, mark]) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) old.removeAttribute(mark);
    const main = document.querySelector('main');
    if (!main) return {name: null, menus: 0};
    const own = new RegExp('^/in/' + user.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '/?$', 'i');
    const pathOf = (a) => { try { return new URL(a.href).pathname; } catch (e) { return ''; } };
    const firstLine = (el) => ((el.innerText || '').split('\n').map((l) => l.trim()).find(Boolean) || '');
    const author = [...main.querySelectorAll('a[href*="/in/"]')]
        .find((a) => own.test(decodeURIComponent(pathOf(a))) && firstLine(a));
    // After the author's link, so a menu of the page's own header is not it.
    const menus = !author ? [] : [...main.querySelectorAll('button[aria-label]')]
        .filter((b) => b.querySelector('svg[id^="ellipses"], [data-test-icon^="ellipses"]'))
        .filter((b) => b.getClientRects().length > 0)
        .filter((b) => author.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (menus.length) menus[0].setAttribute(mark, '');
    // The degree after the name (" • 3rd+") is cut at its bullet, a mark and
    // not a word, in case it shares the name's line.
    const name = author ? firstLine(author).split('\u2022')[0].trim() : null;
    return {name: name || null, menus: menus.length};
}"""

# The open menu's items: the hide item (eye-slash) marked, and whether the
# unfollow item (circle-xmark) is there instead.
HIDE_ITEM_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) old.removeAttribute(mark);
    const items = [...document.querySelectorAll('[role="menu"] [role="menuitem"]')]
        .filter((el) => el.getClientRects().length > 0);
    const icon = (el, prefix) => [...el.querySelectorAll('svg[id], [data-test-icon]')]
        .some((n) => (n.id || n.getAttribute('data-test-icon') || '').startsWith(prefix));
    const hide = items.filter((el) => icon(el, 'eye-slash'));
    if (hide.length === 1) hide[0].setAttribute(mark, '');
    return {items: items.length, hide: hide.length,
            unfollow: items.filter((el) => icon(el, 'circle-xmark')).length};
}"""

# The settings list of people unfollowed or muted: each name, the count the
# page states, and the Follow button of the one named *name* marked.
UNFOLLOWED_JS = r"""([name, mark]) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) old.removeAttribute(mark);
    const card = document.querySelector('[data-test-unfollowed-card]');
    if (!card) return null;
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const rows = [...card.querySelectorAll('li')].map((li) => ({
        li, name: (li.querySelector('[data-test-profile-card-name]') || {}).innerText || ''}));
    const stated = ((card.querySelector('[data-test-unfollowed-card__primary-desc]') || {})
        .innerText || '').match(/\d[\d.,\s]*/);
    const hits = rows.filter((r) => name && norm(r.name) === norm(name));
    if (hits.length === 1) {
        const button = hits[0].li.querySelector('button[data-test-unfollowed-member__follow-btn]');
        if (button) button.setAttribute(mark, '');
    }
    return {names: rows.map((r) => r.name.trim()), hits: hits.length,
            stated: stated ? parseInt(stated[0].replace(/\D/g, ''), 10) : null};
}"""


def _activity_url(username: str) -> str:
    return person_profile_url(username, "/recent-activity/all/")


class MuteActions:
    """Mute and unmute people for the signed-in member."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def set_person_muted(self, username: str, muted: bool) -> dict[str, Any]:
        """Mute or unmute one person, leaving them as asked.

        The settings list is read first, so a person already in the asked
        state is left alone. Either change is confirmed by the list again.

        Returns:
            {url, username, name, muted, status} where status is "muted",
            "unmuted", "already_muted" or "already_unmuted".
        """
        username = normalize_person_identifier(username)
        url = person_profile_url(username, "/")
        name = await self._open_activity(username)
        listed = await self._read_list(name)
        if listed["hits"] > 1:
            raise LinkedInOperationError(
                f"{listed['hits']} people named {name!r} are in LinkedIn's list "
                "of people unfollowed or muted, so which one is "
                f"{username} cannot be told. Nothing was changed."
            )
        if bool(listed["hits"]) == muted:
            status: MuteStatus = "already_muted" if muted else "already_unmuted"
            return _result(url, username, name, muted, status)
        if muted:
            await self._mute(username, name)
        else:
            await self._click(f"button[{_FOLLOW_MARK}]")
        after = None
        for attempt in range(_LIST_READS):
            await self._session.delay(NAV_DELAY if attempt == 0 else _LIST_SETTLE)
            try:
                after = await self._read_list(name)
            except LinkedInOperationError as exc:
                if type(exc) is not LinkedInOperationError:
                    raise
                raise ActionOutcomeError(
                    f"{'Mute' if muted else 'Unmute'} was chosen for {username}, "
                    f"but the settings list could not be read afterwards ({exc})."
                ) from exc
            if bool(after["hits"]) == muted:
                break
        assert after is not None
        if bool(after["hits"]) != muted:
            raise ActionOutcomeError(
                f"{'Mute' if muted else 'Unmute'} was chosen for {username}, but "
                "LinkedIn's list of people unfollowed or muted "
                f"{'does not list' if muted else 'still lists'} {name!r}, so "
                "LinkedIn may not have kept it. Calling this again is safe: it "
                "reads the list first."
            )
        return _result(url, username, name, muted, "muted" if muted else "unmuted")

    async def _open_activity(self, username: str) -> str:
        """Open the person's activity page and read their name there."""
        page = self._session.page
        await self._navigator._navigate_to_page(_activity_url(username))
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        args = [username, _MENU_MARK]
        deadline = self._session.monotonic() + _READY_TIMEOUT
        found = await page.evaluate(ACTIVITY_JS, args)
        while (
            not (found["name"] and found["menus"])
            and self._session.monotonic() < deadline
        ):
            await self._session.delay(_POLL)
            found = await page.evaluate(ACTIVITY_JS, args)
        if not found["name"]:
            raise LinkedInOperationError(
                f"The activity page of {username} did not show their name, so "
                "they cannot be found in LinkedIn's settings list. Nothing was "
                "changed."
            )
        return re.sub(r"\s+", " ", found["name"]).strip()

    async def _mute(self, username: str, name: str) -> None:
        """Choose Hide posts from the first post's menu on the activity page."""
        page = self._session.page
        await self._navigator._navigate_to_page(_activity_url(username))
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        args = [username, _MENU_MARK]
        deadline = self._session.monotonic() + _READY_TIMEOUT
        found = await page.evaluate(ACTIVITY_JS, args)
        while not found["menus"] and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await page.evaluate(ACTIVITY_JS, args)
        if not found["menus"]:
            raise LinkedInOperationError(
                f"The activity page of {username} shows no post with a menu, and "
                "a person is muted from one of their posts. Nothing was changed."
            )
        await self._click(f"button[{_MENU_MARK}]")
        deadline = self._session.monotonic() + _MENU_TIMEOUT
        item = await page.evaluate(HIDE_ITEM_JS, _ITEM_MARK)
        while (
            item["hide"] != 1
            and not item["unfollow"]
            and self._session.monotonic() < deadline
        ):
            await self._session.delay(_POLL)
            item = await page.evaluate(HIDE_ITEM_JS, _ITEM_MARK)
        if item["hide"] != 1:
            await page.keyboard.press("Escape")
            if item["unfollow"]:
                raise LinkedInOperationError(
                    f"You follow {username}: LinkedIn offers Unfollow for their "
                    "posts, not hiding them. Use unfollow_person to stop seeing "
                    "their posts. Nothing was changed."
                )
            raise LinkedInOperationError(
                f"The post menu on {username}'s activity page showed "
                f"{item['items']} items and no hide item. It was closed and "
                "nothing was chosen."
            )
        await self._click(f"[{_ITEM_MARK}]")

    async def _read_list(self, name: str) -> dict[str, Any]:
        """Read the settings list of people unfollowed or muted, all of it."""
        page = self._session.page
        await self._navigator._navigate_to_page(UNFOLLOWED_URL)
        await self._session.check_rate_limit()
        args = [name, _FOLLOW_MARK]
        deadline = self._session.monotonic() + _READY_TIMEOUT
        found = await page.evaluate(UNFOLLOWED_JS, args)
        while found is None and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await page.evaluate(UNFOLLOWED_JS, args)
        if found is None:
            raise LinkedInOperationError(
                f"{UNFOLLOWED_URL} did not show LinkedIn's list of people "
                "unfollowed or muted."
            )
        for _ in range(_LIST_SCROLLS):
            if (
                found["hits"]
                or found["stated"] is None
                or len(found["names"]) >= found["stated"]
            ):
                break
            await page.mouse.wheel(0, 4000)
            await self._session.delay(1.0)
            found = await page.evaluate(UNFOLLOWED_JS, args)
        if (
            not found["hits"]
            and found["stated"] is not None
            and len(found["names"]) < found["stated"]
        ):
            raise LinkedInOperationError(
                f"LinkedIn's list of people unfollowed or muted states "
                f"{found['stated']} people but showed {len(found['names'])}, so "
                f"whether {name!r} is among them cannot be told. Nothing was "
                "changed."
            )
        return found

    async def _click(self, selector: str) -> None:
        async with linkedin_element(
            self._session.page.locator(selector), timeout=5000
        ) as element:
            await element.click(timeout=5000)


def _result(
    url: str, username: str, name: str, muted: bool, status: MuteStatus
) -> dict[str, Any]:
    return {
        "url": url,
        "username": username,
        "name": name,
        "muted": muted,
        "status": status,
    }
