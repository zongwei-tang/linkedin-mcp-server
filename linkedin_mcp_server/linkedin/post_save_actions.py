"""Write actions on one post: save it to your saved posts, and unsave it.

Measured 2026-11-22 on a `/feed/update/<urn>/` page, English locale, by saving
a post and unsaving it again, each confirmed from the saved-posts list and the
unsave again from a fresh browser:

- The post's control menu opens from the first expandable button of the post
  (`button[aria-expanded]` with an `aria-label`), outside every comment
  (`article[data-id]`), ahead of the post's reactions caret
  (`data-finite-scroll-hotkey="l"`), and not one of the action-bar shortcuts
  or the "see more reactions" jump link. Its icon carries no identifier.
- The menu's items are `role="button"` elements, each around an icon with a
  `data-test-icon`. Save is the one around `bookmark-outline-medium`; once the
  post is saved the same item is around `bookmark-fill-medium`. The menu
  closes on Escape.

So the state is read from the icon's name, an identifier and not a word, and
no label is read. Saving is private: the author is not told.

Verified live 2026-11-22 through the MCP tools, across three server
processes with the browser closed between them: save (saved), save again
(already_saved), unsave (unsaved), unsave again (already_unsaved).
"""

from __future__ import annotations

from typing import Any, Literal, NoReturn

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin.identifiers import (
    normalize_post_reference,
    post_page_url,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

SaveStatus = Literal["saved", "unsaved", "already_saved", "already_unsaved"]

_MENU_MARK = "data-mcp-post-menu"
_BEFORE_MARK = "data-mcp-before-post-menu"
_ITEM_MARK = "data-mcp-save-item"
_POST_READY_TIMEOUT = 10.0
_MENU_TIMEOUT = 5.0
_POLL = 0.25

# Marks the post's control-menu button, once its action bar has rendered.
POST_MENU_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const outsideComments = (el) => !el.closest('article[data-id]');
    const carets = [...document.querySelectorAll(
        'button[data-finite-scroll-hotkey="l"]'
    )].filter(outsideComments);
    const result = {carets: carets.length, menus: 0};
    if (carets.length !== 1) return result;
    const caret = carets[0];
    const menus = [...document.querySelectorAll('button[aria-expanded][aria-label]')]
        .filter(outsideComments)
        .filter((button) => !button.hasAttribute('data-finite-scroll-hotkey'))
        .filter((button) => !button.hasAttribute('data-jump-link-target'))
        .filter((button) => button.compareDocumentPosition(caret)
            & Node.DOCUMENT_POSITION_FOLLOWING);
    result.menus = menus.length;
    if (menus.length) menus[0].setAttribute(mark, '');
    return result;
}"""

# Marks every control on the page, so the menu's items are the ones the click
# adds.
MARK_CONTROLS_JS = r"""(before) => {
    for (const el of document.querySelectorAll('button, [role="button"]')) {
        el.setAttribute(before, '');
    }
    return true;
}"""

# The menu's save item: the one new, visible role=button around a bookmark
# icon. ``saved`` is whether that icon is the filled one.
SAVE_ITEM_JS = r"""(opts) => {
    const {before, mark} = opts;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const items = [...document.querySelectorAll('[role="button"]:not([' + before + '])')]
        .filter((el) => el.getClientRects().length > 0)
        .map((el) => [el, [...el.querySelectorAll('[data-test-icon]')]
            .map((icon) => icon.getAttribute('data-test-icon') || '')
            .find((name) => name.startsWith('bookmark-'))])
        .filter(([, name]) => name);
    const result = {items: items.length, saved: null};
    if (items.length !== 1) return result;
    const [el, name] = items[0];
    if (name.startsWith('bookmark-fill')) result.saved = true;
    else if (name.startsWith('bookmark-outline')) result.saved = false;
    else return result;
    el.setAttribute(mark, '');
    return result;
}"""


class PostSaveActions:
    """Own saving and unsaving a post for the signed-in member."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def set_post_saved(self, post: str, saved: bool) -> dict[str, Any]:
        """Save or unsave one post, leaving it as asked.

        The menu is read before anything in it is chosen, so a post already in
        the asked state is left alone and a retry is safe. After the click the
        post is loaded again and its menu must show the new state.

        Returns:
            {url, post, saved, status} where status is "saved", "unsaved",
            "already_saved" or "already_unsaved".
        """
        path = normalize_post_reference(post)
        url = post_page_url(path)
        before = await self._open_menu(url)
        if before == saved:
            await self._session.page.keyboard.press("Escape")
            status: SaveStatus = "already_saved" if saved else "already_unsaved"
            return _result(url, path, saved, status)

        await self._click(f"[{_ITEM_MARK}]")
        await self._session.delay(NAV_DELAY)
        try:
            after = await self._open_menu(url, clicked=True)
        finally:
            await self._session.page.keyboard.press("Escape")
        if after != saved:
            raise ActionOutcomeError(
                f"{'Save' if saved else 'Unsave'} was chosen on {url}, but after "
                "a reload its menu still showed the post "
                f"{'unsaved' if saved else 'saved'}, so LinkedIn may not have "
                "kept it. Calling this again is safe: it reads the state first."
            )
        return _result(url, path, saved, "saved" if saved else "unsaved")

    async def _open_menu(self, url: str, *, clicked: bool = False) -> bool:
        """Open the post's control menu and read whether the post is saved.

        ``clicked`` is the reload after a click: the post may already have
        changed, so a menu that cannot be read is an unknown outcome rather
        than nothing done. Refusals (a sign-in wall, a rate limit, a bot
        check) are raised as they are either way, never wrapped.
        """
        page = self._session.page
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        deadline = self._session.monotonic() + _POST_READY_TIMEOUT
        found = await page.evaluate(POST_MENU_JS, _MENU_MARK)
        while not found["menus"] and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await page.evaluate(POST_MENU_JS, _MENU_MARK)
        if not found["menus"]:
            self._fail(
                clicked,
                f"The control menu of {url} could not be found: the page showed "
                f"{found['carets']} post reaction controls, where one was "
                "expected, and no menu button ahead of it.",
            )
        await page.evaluate(MARK_CONTROLS_JS, _BEFORE_MARK)
        await self._click(f"button[{_MENU_MARK}]")
        options = {"before": _BEFORE_MARK, "mark": _ITEM_MARK}
        deadline = self._session.monotonic() + _MENU_TIMEOUT
        item = await page.evaluate(SAVE_ITEM_JS, options)
        while item["saved"] is None and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            item = await page.evaluate(SAVE_ITEM_JS, options)
        if item["saved"] is None:
            await page.keyboard.press("Escape")
            self._fail(
                clicked,
                f"The control menu of {url} opened, but {item['items']} save "
                "items were found in it where one was expected. It was closed.",
            )
        return bool(item["saved"])

    @staticmethod
    def _fail(clicked: bool, reason: str) -> NoReturn:
        if clicked:
            raise ActionOutcomeError(
                f"{reason} The choice before this reload went through, so the "
                "post may have changed. Read get_saved_posts before retrying."
            )
        raise LinkedInOperationError(f"{reason} Nothing was chosen.")

    async def _click(self, selector: str) -> None:
        async with linkedin_element(
            self._session.page.locator(selector), timeout=5000
        ) as element:
            await element.click(timeout=5000)


def _result(url: str, path: str, saved: bool, status: SaveStatus) -> dict[str, Any]:
    return {"url": url, "post": path, "saved": saved, "status": status}
