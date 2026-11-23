"""Write actions on a person's profile: follow and unfollow.

Measured 2026-11-20 on the profile of someone the account follows, English
locale:

- The top card's follow control is the first button in `<main>` around a
  `check-outline-small` icon (followed) or `plus-outline-small` (not
  followed) that has the top card's More button, a `button[aria-expanded]`
  with no `aria-label` and no ellipsis icon, within three levels above it.
  Suggested profiles sit in `<aside>`, and a newsletter's Subscribe button
  carries the same plus icon five levels away from More, so neither matches.
- Clicking the followed control opens a confirmation whose new buttons are a
  close button around an `xmark` icon and two without icons, Cancel then
  Unfollow. Unfollow is the second of the two; Escape cancels. The rest of the
  profile can keep rendering buttons while it opens (measured live: 109 new
  plain buttons), so the confirmation is the one container close above its
  buttons that holds its icon close button and exactly two plain ones.

- A profile whose top card shows no follow button (measured 2026-11-23 on
  third-degree members) keeps it in the top card's More menu: a
  `role="menuitem"` around the same two icons. Unfollowing from it asks the
  same confirmation.

The icon names are identifiers, not words, and the confirmation is read by
position, so no label is read.

Verified live 2026-11-20 through the MCP tools on one followed profile:
follow (already_followed, no click), unfollow through the confirmation,
unfollow again (already_unfollowed, no click), follow back, each confirmed by
a reload. Verified again 2026-11-21 across server processes, with the browser
closed between them, after the confirmation fix: the unfollow and the follow
back were both still there in the next process.
"""

from __future__ import annotations

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

FollowStatus = Literal[
    "followed", "unfollowed", "already_followed", "already_unfollowed"
]

_FOLLOW_MARK = "data-mcp-follow"
_BEFORE_MARK = "data-mcp-before-confirm"
_CONFIRM_MARK = "data-mcp-confirm"
_MORE_MARK = "data-mcp-follow-more"
_PROFILE_READY_TIMEOUT = 10.0
#: How long the top card may show no follow button before its More menu is
#: looked in, and how long that menu may take to open.
_MENU_FALLBACK_AFTER = 4.0
_MENU_TIMEOUT = 5.0
_FOLLOW_TIMEOUT = 6.0
_POLL = 0.25

# Finds the top card's follow button, marks it and says which state its icon
# shows. ``candidates`` counts the follow-icon buttons that qualify, so a page
# with none, or a shape where the first one is ambiguous, is reported.
TOP_CARD_FOLLOW_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const STATES = {'check-outline-small': true, 'plus-outline-small': false};
    const iconOf = (button) => {
        for (const el of button.querySelectorAll('svg[id], [data-test-icon]')) {
            const name = el.id || el.getAttribute('data-test-icon');
            if (name in STATES) return name;
        }
        return null;
    };
    const isMore = (button) => !button.querySelector(
        'svg[id^="ellipses"], [data-test-icon^="ellipses"]'
    );
    const main = document.querySelector('main');
    const result = {candidates: 0, followed: null};
    if (!main) return result;
    for (const button of main.querySelectorAll('button')) {
        if (button.closest('aside')) continue;
        const icon = iconOf(button);
        if (!icon) continue;
        let node = button.parentElement;
        for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
            const more = [...node.querySelectorAll(
                'button[aria-expanded]:not([aria-label])'
            )].filter((other) => other !== button && isMore(other));
            if (more.length) {
                result.candidates += 1;
                if (result.candidates === 1) {
                    result.followed = STATES[icon];
                    button.setAttribute(mark, '');
                }
                break;
            }
        }
    }
    return result;
}"""

# The top card's More button, marked, when the top card shows no follow button
# of its own: the first `button[aria-expanded]` without an aria-label and
# without the ellipsis icon in <main>, outside <aside>.
TOP_CARD_MORE_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const main = document.querySelector('main');
    if (!main) return 0;
    const more = [...main.querySelectorAll('button[aria-expanded]:not([aria-label])')]
        .filter((b) => !b.closest('aside'))
        .filter((b) => !b.querySelector('svg[id^="ellipses"], [data-test-icon^="ellipses"]'))
        .filter((b) => b.getClientRects().length > 0);
    if (more.length) more[0].setAttribute(mark, '');
    return more.length;
}"""

# The open More menu's follow item: the one menu item around the follow icons,
# marked, and which state its icon shows.
MENU_FOLLOW_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const STATES = {'check-outline-small': true, 'plus-outline-small': false};
    const items = [...document.querySelectorAll('[role="menu"] [role="menuitem"]')]
        .filter((el) => el.getClientRects().length > 0);
    const hits = items.map((el) => [el, [...el.querySelectorAll('svg[id], [data-test-icon]')]
        .map((n) => n.id || n.getAttribute('data-test-icon')).find((n) => n in STATES)])
        .filter(([, icon]) => icon);
    if (hits.length !== 1) return {items: items.length, followed: null};
    hits[0][0].setAttribute(mark, '');
    return {items: items.length, followed: STATES[hits[0][1]]};
}"""

MARK_BUTTONS_JS = r"""(before) => {
    for (const button of document.querySelectorAll('button')) {
        button.setAttribute(before, '');
    }
    return true;
}"""

# The confirmation's own action: the second of its two new buttons that carry
# no icon. The confirmation is told from buttons rendered elsewhere at the same
# time by its container: the one element, close above them, that holds exactly
# two new plain buttons and a new button around an icon (its close button).
CONFIRM_UNFOLLOW_JS = r"""(opts) => {
    const {before, mark} = opts;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    // Suggestions in <aside> keep rendering too, and are never the dialog.
    const added = [...document.querySelectorAll('button:not([' + before + '])')]
        .filter((button) => !button.closest('aside'));
    const plain = added.filter((button) => !button.querySelector('svg, img'));
    // The close button as measured: around an xmark icon, by id or test hook.
    // An empty id with no test hook names nothing, and must not throw.
    const closers = added.filter((button) => [...button.querySelectorAll(
        'svg[id], [data-test-icon]'
    )].some((icon) => (icon.id || icon.getAttribute('data-test-icon') || '').startsWith('xmark')));
    const dialogs = new Map();
    for (const button of plain) {
        let node = button.parentElement;
        for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
            const inside = plain.filter((other) => node.contains(other));
            if (inside.length > 2) break;
            if (inside.length === 2 && closers.some((closer) => node.contains(closer))) {
                dialogs.set(node, inside);
                break;
            }
        }
    }
    if (dialogs.size === 1) {
        const [actions] = dialogs.values();
        actions[1].setAttribute(mark, '');
    }
    return {plain: plain.length, dialogs: dialogs.size};
}"""


class PersonActions:
    """Own the actions the signed-in member takes on someone else's profile."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def set_person_followed(
        self, username: str, followed: bool
    ) -> dict[str, Any]:
        """Follow or unfollow one person, leaving them as asked.

        The top card is read before anything is clicked, so a person already in
        the asked state is left alone and a retry is safe. Following is one
        click; unfollowing confirms LinkedIn's dialog. Either way the profile
        is loaded again and its top card must show the new state.

        Returns:
            {url, username, followed, status} where status is "followed",
            "unfollowed", "already_followed" or "already_unfollowed".
        """
        username = normalize_person_identifier(username)
        url = person_profile_url(username, "/")
        before = await self._open_and_read(url)
        if before["followed"] == followed:
            if before.get("via_menu"):
                await self._session.page.keyboard.press("Escape")
            status: FollowStatus = (
                "already_followed" if followed else "already_unfollowed"
            )
            return _result(url, username, followed, status)

        page = self._session.page
        if not followed:
            await page.evaluate(MARK_BUTTONS_JS, _BEFORE_MARK)
        # The More menu's item is a menuitem, not a button.
        await self._click(
            f"[{_FOLLOW_MARK}]" if before.get("via_menu") else f"button[{_FOLLOW_MARK}]"
        )
        if not followed:
            await self._confirm_unfollow(url)
        # A menu choice leaves no top card button to watch: the reload below
        # confirms it.
        if not before.get("via_menu") and not await self._wait_for(followed):
            # Whatever the click left open goes, so nothing stays half-asked.
            await page.keyboard.press("Escape")
            raise ActionOutcomeError(
                f"The follow button of {url} was clicked, but the top card did "
                "not change. Read the profile again before retrying."
            )
        await self._session.delay(NAV_DELAY)
        after = await self._open_and_read(url, clicked=True)
        if after.get("via_menu"):
            await page.keyboard.press("Escape")
        if after["followed"] != followed:
            raise ActionOutcomeError(
                f"{url} showed the change after the click, but not after a "
                "reload, so LinkedIn may not have kept it. Calling this again is "
                "safe: it reads the state before clicking."
            )
        status = "followed" if followed else "unfollowed"
        return _result(url, username, followed, status)

    async def _click(self, selector: str) -> None:
        async with linkedin_element(
            self._session.page.locator(selector), timeout=5000
        ) as element:
            await element.click(timeout=5000)

    async def _read(self) -> dict[str, Any]:
        found = await self._session.page.evaluate(TOP_CARD_FOLLOW_JS, _FOLLOW_MARK)
        if not isinstance(found, dict):
            raise LinkedInOperationError("The profile page could not be read")
        return found

    async def _open_and_read(
        self, url: str, *, clicked: bool = False
    ) -> dict[str, Any]:
        """Open the profile and read its top card's follow button.

        ``clicked`` is the reload after a click: the account may already have
        changed, so a top card that cannot be read is an unknown outcome
        rather than nothing done. Refusals (a sign-in wall, a rate limit) are
        raised as they are either way, never wrapped, so they keep their own
        code and recovery.
        """
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        start = self._session.monotonic()
        deadline = start + _PROFILE_READY_TIMEOUT
        found = await self._read()
        tried_menu = False
        while found["followed"] is None and self._session.monotonic() < deadline:
            if (
                not tried_menu
                and not found["candidates"]
                and self._session.monotonic() - start >= _MENU_FALLBACK_AFTER
            ):
                # Once: a menu without the item is not opened again.
                tried_menu = True
                menu = await self._read_more_menu()
                if menu is not None:
                    return menu
            await self._session.delay(_POLL)
            found = await self._read()
        if clicked and (found["followed"] is None or found["candidates"] != 1):
            raise ActionOutcomeError(
                f"{url} showed the change after the click, but after a reload "
                f"its top card showed {found['candidates']} follow buttons "
                "beside a More button where one was expected, so whether "
                "LinkedIn kept it is not confirmed. Read the profile again "
                "before retrying."
            )
        if found["followed"] is None:
            raise LinkedInOperationError(
                f"The follow button of {url} could not be found in its top card "
                "or its More menu. Nothing was clicked."
            )
        if found["candidates"] != 1:
            raise LinkedInOperationError(
                f"{url} shows {found['candidates']} follow buttons beside a More "
                "button, so its top card's could not be told apart. Nothing was "
                "clicked."
            )
        return found

    async def _read_more_menu(self) -> dict[str, Any] | None:
        """Open the top card's More menu and read its follow item, if any.

        Measured 2026-11-23: a profile whose top card shows no follow button
        (a third-degree member's, a first-degree connection's) keeps it in the
        More menu, as a menu item around the same icons. The menu is left open
        with the item marked; None, with the menu closed, when it has none.
        """
        page = self._session.page
        if not await page.evaluate(TOP_CARD_MORE_JS, _MORE_MARK):
            return None
        await self._click(f"button[{_MORE_MARK}]")
        deadline = self._session.monotonic() + _MENU_TIMEOUT
        item = await page.evaluate(MENU_FOLLOW_JS, _FOLLOW_MARK)
        while item["followed"] is None and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            item = await page.evaluate(MENU_FOLLOW_JS, _FOLLOW_MARK)
        if item["followed"] is None:
            await page.keyboard.press("Escape")
            return None
        return {"candidates": 1, "followed": item["followed"], "via_menu": True}

    async def _confirm_unfollow(self, url: str) -> None:
        page = self._session.page
        options = {"before": _BEFORE_MARK, "mark": _CONFIRM_MARK}
        deadline = self._session.monotonic() + _FOLLOW_TIMEOUT
        while True:
            state = await self._read()
            if state["followed"] is False:
                # Unfollowed without asking, which is the outcome asked for.
                return
            found = await page.evaluate(CONFIRM_UNFOLLOW_JS, options)
            if found["dialogs"] == 1:
                break
            if self._session.monotonic() >= deadline:
                await page.keyboard.press("Escape")
                raise LinkedInOperationError(
                    f"Unfollow was clicked on {url}, but no single confirmation "
                    f"with Cancel and Unfollow could be told apart ({found['plain']} "
                    f"new plain buttons, {found['dialogs']} candidate dialogs). It "
                    "was closed; nothing was confirmed."
                )
            await self._session.delay(_POLL)
        await self._click(f"button[{_CONFIRM_MARK}]")

    async def _wait_for(self, followed: bool) -> bool:
        deadline = self._session.monotonic() + _FOLLOW_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            if (await self._read())["followed"] == followed:
                return True
        return False


def _result(
    url: str, username: str, followed: bool, status: FollowStatus
) -> dict[str, Any]:
    return {"url": url, "username": username, "followed": followed, "status": status}
