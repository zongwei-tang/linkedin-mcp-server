"""Write actions on the signed-in member's received invitations: ignore one.

Measured 2026-11-20 on `/mynetwork/invitation-manager/received/`, English
locale. Each pending invitation is one row whose `componentkey` is
`invitation_manager_row_urn:li:invitation:<id>`, holding the sender's profile
link and two buttons, Ignore then Accept in DOM order, neither with an icon.
The row key and the profile link are identifiers, not words; which of the two
buttons is Ignore is read from their order, never from their labels.
"""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin.identifiers import normalize_person_identifier
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.network import INVITATION_URLS
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

_IGNORE_MARK = "data-mcp-ignore"
ROW_KEY_PREFIX = "invitation_manager_row_urn:li:invitation:"
_LIST_READY_TIMEOUT = 10.0
_IGNORE_TIMEOUT = 5.0
_POLL = 0.25

# The sender's row, found by its profile link, with its first button marked.
# Rows nested in another row are the same invitation twice and are skipped.
INVITATION_ROW_JS = r"""(opts) => {
    const {username, mark, prefix} = opts;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const selector = '[componentkey^="' + prefix + '"]';
    const rows = [...document.querySelectorAll(selector)].filter(
        (row) => !row.parentElement || !row.parentElement.closest(selector)
    );
    const want = '/in/' + username.toLowerCase() + '/';
    const pathOf = (anchor) => {
        try {
            const path = new URL(anchor.href, location.href).pathname;
            return decodeURIComponent(path).toLowerCase();
        } catch (error) {
            return '';
        }
    };
    const own = rows.filter((row) => [...row.querySelectorAll('a[href*="/in/"]')]
        .some((anchor) => (pathOf(anchor) + '/').startsWith(want)));
    const result = {
        rows: rows.length, matches: own.length, buttons: 0, invitation: null,
        text: ((document.querySelector('main') || document.body).innerText || '').length,
    };
    if (own.length !== 1) return result;
    result.invitation = own[0].getAttribute('componentkey').slice(prefix.length);
    const buttons = [...own[0].querySelectorAll('button')].filter(
        (button) => !button.closest('a')
    );
    result.buttons = buttons.length;
    if (buttons.length === 2) buttons[0].setAttribute(mark, '');
    return result;
}"""

# Whether a row with this exact invitation key is still on the page.
INVITATION_PRESENT_JS = r"""(key) => ({
    present: !!document.querySelector('[componentkey="' + key + '"]'),
    text: ((document.querySelector('main') || document.body).innerText || '').length,
})"""


class InvitationActions:
    """Own the actions the signed-in member takes on a pending invitation."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def ignore_invitation(self, username: str) -> dict[str, Any]:
        """Ignore the pending invitation one person sent.

        Reads the received list, finds the sender's row by their profile link
        and clicks its first action, which is Ignore. The row has to leave the
        list, and a fresh load of the list must not show that invitation
        again, because a click the page took is not yet a change LinkedIn kept.
        Only the invitations the first load of the list renders are searched.

        Returns:
            {url, username, invitation_id, status: "ignored"}
        """
        username = normalize_person_identifier(username)
        url = INVITATION_URLS["received"]
        row = await self._open_and_find(url, username)
        invitation = str(row["invitation"])
        key = ROW_KEY_PREFIX + invitation

        async with linkedin_element(
            self._session.page.locator(f"button[{_IGNORE_MARK}]"), timeout=5000
        ) as button:
            await button.click(timeout=5000)
        if not await self._wait_until_gone(key):
            raise ActionOutcomeError(
                f"Ignore was clicked on {username}'s invitation, but its row "
                "stayed in the list."
            )
        await self._session.delay(NAV_DELAY)
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        after = await self._presence_once_rendered(key)
        if after["present"]:
            raise ActionOutcomeError(
                f"{username}'s invitation left the list after Ignore, but was "
                "back after a reload, so LinkedIn may not have kept it."
            )
        return {
            "url": url,
            "username": username,
            "invitation_id": invitation,
            "status": "ignored",
        }

    async def _find(self, username: str) -> dict[str, Any]:
        found = await self._session.page.evaluate(
            INVITATION_ROW_JS,
            {"username": username, "mark": _IGNORE_MARK, "prefix": ROW_KEY_PREFIX},
        )
        if not isinstance(found, dict):
            raise LinkedInOperationError("The invitations page could not be read")
        return found

    async def _open_and_find(self, url: str, username: str) -> dict[str, Any]:
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        deadline = self._session.monotonic() + _LIST_READY_TIMEOUT
        found = await self._find(username)
        while found["matches"] != 1 and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await self._find(username)
        if found["matches"] == 0:
            raise LinkedInOperationError(
                f"No pending invitation from {username} is listed: the received "
                f"list showed {found['rows']} invitations, none linking to that "
                "profile. Nothing was clicked."
            )
        if found["matches"] > 1:
            raise LinkedInOperationError(
                f"{found['matches']} invitations link to {username}, where one "
                "was expected. Nothing was clicked."
            )
        if found["buttons"] != 2:
            raise LinkedInOperationError(
                f"{username}'s invitation has {found['buttons']} actions, where "
                "Ignore and Accept were expected. Nothing was clicked."
            )
        return found

    async def _wait_until_gone(self, key: str) -> bool:
        deadline = self._session.monotonic() + _IGNORE_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            state = await self._session.page.evaluate(INVITATION_PRESENT_JS, key)
            if not state["present"]:
                return True
        return False

    async def _presence_once_rendered(self, key: str) -> dict[str, Any]:
        """The row's presence once the reloaded list has rendered any text.

        An empty list is a real answer after the last invitation is ignored,
        so the list is judged by having rendered at all, not by its rows.
        """
        deadline = self._session.monotonic() + _LIST_READY_TIMEOUT
        state = await self._session.page.evaluate(INVITATION_PRESENT_JS, key)
        while (
            not state["present"]
            and state["text"] < 100
            and self._session.monotonic() < deadline
        ):
            await self._session.delay(_POLL)
            state = await self._session.page.evaluate(INVITATION_PRESENT_JS, key)
        if not state["present"] and state["text"] < 100:
            raise ActionOutcomeError(
                "The invitations list did not render after the reload, so the "
                "ignore could not be confirmed. Check get_invitations before "
                "calling again."
            )
        return state
