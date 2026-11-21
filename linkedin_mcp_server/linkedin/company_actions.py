"""Write actions on a company page: follow and unfollow.

Measured 2026-11-20 on a company page the account follows, through the
server's own browser, English locale:

- The page's follow control is a `button[aria-pressed]` around a
  `check-small` icon when followed (`aria-pressed="true"`), with the page's
  overflow menu, a `button[aria-expanded]` around `overflow-web-ios-small`,
  within a few levels above it. The page's posts carry their authors' Follow
  buttons, also `button[aria-pressed]` around `add-small`, but no such
  overflow button beside them.
- Clicking the followed control opens a `role="alertdialog"` whose confirm
  button carries `data-test-dialog-primary-btn` and whose Cancel carries
  `data-test-dialog-secondary-btn`. Escape cancels.

`aria-pressed`, icon names and test hooks are not words, so no label is read.

Verified live 2026-11-21 through the MCP tools on one followed page: follow
(already_followed, no click), unfollow through the alertdialog, unfollow again
(already_unfollowed, no click), follow back, each confirmed by a reload.
"""

from __future__ import annotations

from typing import Any, Literal

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin.identifiers import (
    company_page_url,
    normalize_company_identifier,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

CompanyFollowStatus = Literal[
    "followed", "unfollowed", "already_followed", "already_unfollowed"
]

_FOLLOW_MARK = "data-mcp-company-follow"
_PAGE_READY_TIMEOUT = 10.0
_FOLLOW_TIMEOUT = 6.0
_POLL = 0.25

# The page's own follow button, marked, and whether it is pressed.
COMPANY_FOLLOW_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const iconsOf = (el) => [...el.querySelectorAll('svg[id], [data-test-icon]')]
        .map((node) => node.id || node.getAttribute('data-test-icon'));
    const FOLLOW_ICONS = ['check-small', 'add-small'];
    const main = document.querySelector('main');
    const result = {found: false, followed: null};
    if (!main) return result;
    for (const button of main.querySelectorAll('button[aria-pressed]')) {
        if (button.closest('aside')) continue;
        if (!iconsOf(button).some((name) => FOLLOW_ICONS.includes(name))) continue;
        let node = button.parentElement;
        for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
            const overflow = [...node.querySelectorAll('button[aria-expanded]')].some(
                (other) => iconsOf(other).includes('overflow-web-ios-small')
            );
            if (overflow) {
                result.found = true;
                result.followed = button.getAttribute('aria-pressed') === 'true';
                button.setAttribute(mark, '');
                return result;
            }
        }
    }
    return result;
}"""

_CONFIRM_SELECTOR = '[role="alertdialog"] button[data-test-dialog-primary-btn]'


class CompanyActions:
    """Own the actions the signed-in member takes on a company page."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def set_company_followed(
        self, company: str, followed: bool
    ) -> dict[str, Any]:
        """Follow or unfollow one company page, leaving it as asked.

        The page's follow button is read before anything is clicked, so a page
        already in the asked state is left alone and a retry is safe.
        Unfollowing accepts LinkedIn's confirmation. Either way the page is
        loaded again and must show the new state.

        Returns:
            {url, company, followed, status} where status is "followed",
            "unfollowed", "already_followed" or "already_unfollowed".
        """
        company = normalize_company_identifier(company)
        url = company_page_url(company, "/")
        before = await self._open_and_read(url)
        if before["followed"] == followed:
            status: CompanyFollowStatus = (
                "already_followed" if followed else "already_unfollowed"
            )
            return _result(url, company, followed, status)

        await self._click(f"button[{_FOLLOW_MARK}]")
        if not followed:
            await self._confirm_unfollow(url)
        if not await self._wait_for(followed):
            raise LinkedInOperationError(
                f"The follow button of {url} was clicked, but the page did not "
                "change. Read it again before retrying."
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
        return _result(url, company, followed, status)

    async def _click(self, selector: str) -> None:
        async with linkedin_element(
            self._session.page.locator(selector), timeout=5000
        ) as element:
            await element.click(timeout=5000)

    async def _read(self) -> dict[str, Any]:
        found = await self._session.page.evaluate(COMPANY_FOLLOW_JS, _FOLLOW_MARK)
        if not isinstance(found, dict):
            raise LinkedInOperationError("The company page could not be read")
        return found

    async def _open_and_read(self, url: str) -> dict[str, Any]:
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        deadline = self._session.monotonic() + _PAGE_READY_TIMEOUT
        found = await self._read()
        while not found["found"] and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await self._read()
        if not found["found"]:
            raise LinkedInOperationError(
                f"The follow button of {url} could not be found. Nothing was clicked."
            )
        return found

    async def _confirm_unfollow(self, url: str) -> None:
        page = self._session.page
        confirm = page.locator(_CONFIRM_SELECTOR)
        deadline = self._session.monotonic() + _FOLLOW_TIMEOUT
        while True:
            if (await self._read())["followed"] is False:
                # Unfollowed without asking, which is the outcome asked for.
                return
            if await confirm.count() == 1:
                break
            if self._session.monotonic() >= deadline:
                await page.keyboard.press("Escape")
                raise LinkedInOperationError(
                    f"Unfollow was clicked on {url}, but no single confirmation "
                    "button appeared. Anything that opened was closed; nothing "
                    "was confirmed."
                )
            await self._session.delay(_POLL)
        await self._click(_CONFIRM_SELECTOR)

    async def _wait_for(self, followed: bool) -> bool:
        deadline = self._session.monotonic() + _FOLLOW_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            if (await self._read())["followed"] == followed:
                return True
        return False


def _result(
    url: str, company: str, followed: bool, status: CompanyFollowStatus
) -> dict[str, Any]:
    return {"url": url, "company": company, "followed": followed, "status": status}
