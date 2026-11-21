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
  Unfollow. Unfollow is the second of the two; Escape cancels.

The icon names are identifiers, not words, and the confirmation is read by
position, so no label is read.

Verified live 2026-11-20 through the MCP tools on one followed profile:
follow (already_followed, no click), unfollow through the confirmation,
unfollow again (already_unfollowed, no click), follow back, each confirmed by
a reload.
"""

from __future__ import annotations

from typing import Any, Literal

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import LinkedInOperationError
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
_PROFILE_READY_TIMEOUT = 10.0
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

MARK_BUTTONS_JS = r"""(before) => {
    for (const button of document.querySelectorAll('button')) {
        button.setAttribute(before, '');
    }
    return true;
}"""

# The confirmation's own action: the second of the new buttons that carry no
# icon (the close button carries one).
CONFIRM_UNFOLLOW_JS = r"""(opts) => {
    const {before, mark} = opts;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const plain = [...document.querySelectorAll('button:not([' + before + '])')]
        .filter((button) => !button.querySelector('svg, img'));
    if (plain.length === 2) plain[1].setAttribute(mark, '');
    return {plain: plain.length};
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
            status: FollowStatus = (
                "already_followed" if followed else "already_unfollowed"
            )
            return _result(url, username, followed, status)

        page = self._session.page
        if not followed:
            await page.evaluate(MARK_BUTTONS_JS, _BEFORE_MARK)
        await self._click(f"button[{_FOLLOW_MARK}]")
        if not followed:
            await self._confirm_unfollow(url)
        if not await self._wait_for(followed):
            raise LinkedInOperationError(
                f"The follow button of {url} was clicked, but the top card did "
                "not change. Read the profile again before retrying."
            )
        await self._session.delay(NAV_DELAY)
        after = await self._open_and_read(url)
        if after["followed"] != followed:
            raise LinkedInOperationError(
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

    async def _open_and_read(self, url: str) -> dict[str, Any]:
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        deadline = self._session.monotonic() + _PROFILE_READY_TIMEOUT
        found = await self._read()
        while found["followed"] is None and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await self._read()
        if found["followed"] is None:
            raise LinkedInOperationError(
                f"The follow button of {url} could not be found in its top card. "
                "A first-degree connection keeps Follow under the More menu, "
                "which this does not open. Nothing was clicked."
            )
        return found

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
            if found["plain"] == 2:
                break
            if self._session.monotonic() >= deadline:
                await page.keyboard.press("Escape")
                raise LinkedInOperationError(
                    f"Unfollow was clicked on {url}, but its confirmation showed "
                    f"{found['plain']} plain buttons where Cancel and Unfollow "
                    "were expected. It was closed; nothing was confirmed."
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
