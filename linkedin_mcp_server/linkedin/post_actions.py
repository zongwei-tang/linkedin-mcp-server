"""Write actions on one post: like and unlike.

A post page renders the post's action bar once and an action bar per comment.
Measured 2026-11-20 on a `/feed/update/<urn>/` page, English locale:

- The post's own controls carry `data-finite-scroll-hotkey`, the keyboard
  shortcut LinkedIn binds to each: `l` on the reactions-menu caret beside Like,
  `c` Comment, `r` Repost, `s` Send. A shortcut is a key, not a word, so it is
  the same in every locale.
- The Like button itself carries no shortcut. It sits beside the `l` caret and
  is the one `button[aria-pressed]` there; `aria-pressed` is whether the
  signed-in member has reacted.
- Each comment is an `article[data-id]` with its own `l` caret and its own
  `aria-pressed` Like, so everything inside one is excluded.

No label is read: the Like button's text and `aria-label` are translated.

Verified live 2026-11-20 through the MCP tools on one post: like, like again
(already_liked, no click), unlike, unlike again (already_unliked, no click),
each confirmed by a reload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin.identifiers import (
    normalize_post_reference,
    post_page_url,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

LikeStatus = Literal["liked", "unliked", "already_liked", "already_unliked"]

_LIKE_MARK = "data-mcp-like"
# How long a post page gets to render its action bar, and a click to show.
_POST_READY_TIMEOUT = 10.0
_LIKE_TIMEOUT = 5.0
_POLL = 0.25

# Finds the post's own Like button and marks it. Returns how many post-level
# carets and Like buttons it saw, so a page that does not match the measured
# shape is reported as such rather than clicked.
POST_LIKE_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const outsideComments = (el) => !el.closest('article[data-id]');
    const carets = [...document.querySelectorAll(
        'button[data-finite-scroll-hotkey="l"]'
    )].filter(outsideComments);
    const result = {carets: carets.length, likes: 0, pressed: null};
    if (carets.length !== 1) return result;
    let likes = [];
    let node = carets[0].parentElement;
    for (let depth = 0; node && depth < 3; depth++, node = node.parentElement) {
        likes = [...node.querySelectorAll('button[aria-pressed]')].filter(
            (button) => button !== carets[0] && outsideComments(button)
        );
        if (likes.length) break;
    }
    result.likes = likes.length;
    if (likes.length !== 1) return result;
    result.pressed = likes[0].getAttribute('aria-pressed') === 'true';
    likes[0].setAttribute(mark, '');
    return result;
}"""


@dataclass(frozen=True)
class _LikeControl:
    carets: int
    likes: int
    pressed: bool | None

    @property
    def found(self) -> bool:
        return self.carets == 1 and self.likes == 1 and self.pressed is not None


class PostActions:
    """Own the actions the signed-in member takes on someone's post."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def set_post_liked(self, post: str, liked: bool) -> dict[str, Any]:
        """Like or unlike one post, leaving it as asked whatever it was before.

        The post's state is read before anything is clicked, so a post already
        in the asked state is reported as such and left alone, and a retry is
        safe. After a click the page is loaded again and must show the new
        state, because a click the page took is not yet a change LinkedIn kept.

        Args:
            post: A post URN or either permalink form, as get_post_details
                takes it.
            liked: True to like the post, False to remove the like.

        Returns:
            {url, post, liked, status} where status is "liked", "unliked",
            "already_liked" or "already_unliked".
        """
        path = normalize_post_reference(post)
        url = post_page_url(path)
        before = await self._open_and_read(url)
        if before.pressed == liked:
            status: LikeStatus = "already_liked" if liked else "already_unliked"
            return {"url": url, "post": path, "liked": liked, "status": status}

        async with linkedin_element(
            self._session.page.locator(f"button[{_LIKE_MARK}]"), timeout=5000
        ) as button:
            await button.click(timeout=5000)
        if not await self._wait_for(liked):
            raise LinkedInOperationError(
                f"The Like button of {url} was clicked, but the post did not "
                "change. Read it again before retrying."
            )
        await self._session.delay(NAV_DELAY)
        after = await self._open_and_read(url)
        if after.pressed != liked:
            raise LinkedInOperationError(
                f"{url} showed the change after the click, but not after a "
                "reload, so LinkedIn may not have kept it. Calling this again "
                "is safe: it reads the state before clicking."
            )
        status = "liked" if liked else "unliked"
        return {"url": url, "post": path, "liked": liked, "status": status}

    async def _read(self) -> _LikeControl:
        found = await self._session.page.evaluate(POST_LIKE_JS, _LIKE_MARK)
        if not isinstance(found, dict):
            raise LinkedInOperationError("The post page could not be read")
        return _LikeControl(
            carets=int(found.get("carets", 0)),
            likes=int(found.get("likes", 0)),
            pressed=found.get("pressed"),
        )

    async def _open_and_read(self, url: str) -> _LikeControl:
        """Open the post and read its Like button once the action bar renders."""
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        deadline = self._session.monotonic() + _POST_READY_TIMEOUT
        control = await self._read()
        while not control.found and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            control = await self._read()
        if not control.found:
            raise LinkedInOperationError(
                f"The Like button of {url} could not be found: the page showed "
                f"{control.carets} post reaction controls and {control.likes} "
                "Like buttons beside them, where one of each was expected. "
                "Nothing was clicked."
            )
        return control

    async def _wait_for(self, liked: bool) -> bool:
        deadline = self._session.monotonic() + _LIKE_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            control = await self._read()
            if control.found and control.pressed == liked:
                return True
        return False
