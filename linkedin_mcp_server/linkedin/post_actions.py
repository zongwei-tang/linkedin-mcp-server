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

Reactions other than Like, measured 2026-11-20 by choosing Celebrate and
taking it back: the `l` caret opens six new buttons, Like, Celebrate,
Support, Love, Insightful, Funny in that order, with no icon and only a
translated label each, so a reaction is chosen by its position. Once reacted,
the post's button holds an image whose class carries LinkedIn's own reaction
type, `data-test-reactions-icon-type-PRAISE` for Celebrate, which is a code
rather than a word and is what confirms the reaction. Clicking that button
takes any reaction back.

The caret and the menu's buttons are hidden until hovered, so a real click
waits forever for them to become visible (measured live: "element is not
visible"). They are clicked in the page instead, after the element's own
document is checked to be LinkedIn's, which is how the reaction was chosen
when it was measured.
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
Reaction = Literal["like", "celebrate", "support", "love", "insightful", "funny"]
ReactionStatus = Literal["reacted", "already_reacted"]

#: The reactions menu, in the order it lists them.
REACTIONS: tuple[Reaction, ...] = (
    "like",
    "celebrate",
    "support",
    "love",
    "insightful",
    "funny",
)
#: The type code LinkedIn writes on a reacted post's icon for each reaction.
#: PRAISE was read live; the others are the codes LinkedIn's reaction icons
#: carry in the feed (like-, praise-, empathy-, interest-consumption).
REACTION_TYPES: dict[Reaction, str] = {
    "like": "LIKE",
    "celebrate": "PRAISE",
    "support": "APPRECIATION",
    "love": "EMPATHY",
    "insightful": "INTEREST",
    "funny": "ENTERTAINMENT",
}

_LIKE_MARK = "data-mcp-like"
_CARET_MARK = "data-mcp-like-caret"
_BEFORE_MARK = "data-mcp-before-menu"
_CHOICE_MARK = "data-mcp-reaction"
# How long a post page gets to render its action bar, and a click to show.
_POST_READY_TIMEOUT = 10.0
_LIKE_TIMEOUT = 5.0
_MENU_TIMEOUT = 5.0
_POLL = 0.25

# Finds the post's own Like button and marks it. Returns how many post-level
# carets and Like buttons it saw, so a page that does not match the measured
# shape is reported as such rather than clicked.
POST_LIKE_JS = r"""(mark) => {
    for (const name of [mark, mark + '-caret']) {
        for (const old of document.querySelectorAll('[' + name + ']')) {
            old.removeAttribute(name);
        }
    }
    const outsideComments = (el) => !el.closest('article[data-id]');
    const carets = [...document.querySelectorAll(
        'button[data-finite-scroll-hotkey="l"]'
    )].filter(outsideComments);
    const result = {carets: carets.length, likes: 0, pressed: null, reaction: null};
    if (carets.length !== 1) return result;
    carets[0].setAttribute(mark + '-caret', '');
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
    for (const el of likes[0].querySelectorAll('*')) {
        const code = (el.getAttribute('class') || '').match(
            /data-test-reactions-icon-type-([A-Z_]+)/
        );
        if (code) {
            result.reaction = code[1];
            break;
        }
    }
    likes[0].setAttribute(mark, '');
    return result;
}"""

# A click dispatched in the page, for controls that only show on hover.
IN_PAGE_CLICK_JS = "(element) => element.click()"

# Marks every button on the page, so the reactions menu's buttons are the ones
# the caret click adds.
REACTION_MENU_PREPARE_JS = r"""(before) => {
    for (const button of document.querySelectorAll('button')) {
        button.setAttribute(before, '');
    }
    return true;
}"""

# Marks the reaction at ``index`` in the menu the caret click added. Other
# parts of the page keep rendering buttons while the menu opens (measured live:
# 12 new buttons), so the menu is the one element, close above them, that
# holds exactly six of the new buttons; its buttons are taken in DOM order.
REACTION_CHOICE_JS = r"""(opts) => {
    const {before, mark, index} = opts;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const added = [...document.querySelectorAll('button:not([' + before + '])')];
    const menus = new Map();
    for (const button of added) {
        let node = button.parentElement;
        for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
            const inside = added.filter((other) => node.contains(other));
            if (inside.length === 6) {
                menus.set(node, inside);
                break;
            }
            if (inside.length > 6) break;
        }
    }
    if (menus.size === 1) {
        const [reactions] = menus.values();
        reactions[index].setAttribute(mark, '');
    }
    return {added: added.length, menus: menus.size};
}"""


@dataclass(frozen=True)
class _LikeControl:
    carets: int
    likes: int
    pressed: bool | None
    reaction: str | None = None

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
            reaction=found.get("reaction"),
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

    async def set_post_reaction(self, post: str, reaction: Reaction) -> dict[str, Any]:
        """Leave one post with exactly this reaction from the signed-in member.

        A post already showing that reaction is left alone. Like on a post with
        no reaction is the Like button itself; every other case chooses from
        the reactions menu, by position. The post must then show the
        reaction's type code, after the click and again after a reload.

        Returns:
            {url, post, reaction, status: "reacted" | "already_reacted"}
        """
        if reaction not in REACTION_TYPES:
            raise ValueError(f"reaction must be one of {list(REACTIONS)}")
        path = normalize_post_reference(post)
        url = post_page_url(path)
        before = await self._open_and_read(url)
        if _shows(before, reaction):
            return _reaction_result(url, path, reaction, "already_reacted")

        if reaction == "like" and not before.pressed:
            await self._click(f"button[{_LIKE_MARK}]")
        else:
            await self._choose_from_menu(url, reaction)
        if not await self._wait_until(lambda control: _shows(control, reaction)):
            current = await self._read()
            raise LinkedInOperationError(
                f"{reaction!r} was chosen on {url}, but the post shows "
                f"{_described(current)}. Read it again before retrying."
            )
        await self._session.delay(NAV_DELAY)
        after = await self._open_and_read(url)
        if not _shows(after, reaction):
            raise LinkedInOperationError(
                f"{url} showed {reaction!r} after the click, but "
                f"{_described(after)} after a reload, so LinkedIn may not have "
                "kept it."
            )
        return _reaction_result(url, path, reaction, "reacted")

    async def _click(self, selector: str, *, in_page: bool = False) -> None:
        async with linkedin_element(
            self._session.page.locator(selector), timeout=5000
        ) as element:
            if in_page:
                await element.evaluate(IN_PAGE_CLICK_JS)
            else:
                await element.click(timeout=5000)

    async def _choose_from_menu(self, url: str, reaction: Reaction) -> None:
        page = self._session.page
        await page.evaluate(REACTION_MENU_PREPARE_JS, _BEFORE_MARK)
        await self._click(f"button[{_CARET_MARK}]", in_page=True)
        options = {
            "before": _BEFORE_MARK,
            "mark": _CHOICE_MARK,
            "index": REACTIONS.index(reaction),
        }
        deadline = self._session.monotonic() + _MENU_TIMEOUT
        found = await page.evaluate(REACTION_CHOICE_JS, options)
        while found["menus"] != 1 and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await page.evaluate(REACTION_CHOICE_JS, options)
        if found["menus"] != 1:
            await page.keyboard.press("Escape")
            raise LinkedInOperationError(
                f"The reactions menu of {url} could not be told apart: of "
                f"{found['added']} new buttons, {found['menus']} groups of six "
                "were found where one was expected. Nothing was chosen."
            )
        await self._click(f"button[{_CHOICE_MARK}]", in_page=True)

    async def _wait_until(self, done) -> bool:
        deadline = self._session.monotonic() + _LIKE_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            control = await self._read()
            if control.found and done(control):
                return True
        return False

    async def _wait_for(self, liked: bool) -> bool:
        deadline = self._session.monotonic() + _LIKE_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            control = await self._read()
            if control.found and control.pressed == liked:
                return True
        return False


def _shows(control: _LikeControl, reaction: Reaction) -> bool:
    """Whether the post shows exactly ``reaction`` from the signed-in member.

    A liked post's button was not read live with an icon, so Like is also
    accepted from a pressed button that carries no type code at all.
    """
    if not control.pressed:
        return False
    if control.reaction == REACTION_TYPES[reaction]:
        return True
    return reaction == "like" and control.reaction is None


def _described(control: _LikeControl) -> str:
    if not control.found:
        return "no readable reaction button"
    if not control.pressed:
        return "no reaction"
    return f"reaction type {control.reaction or 'without a type code'}"


def _reaction_result(
    url: str, path: str, reaction: Reaction, status: ReactionStatus
) -> dict[str, Any]:
    return {"url": url, "post": path, "reaction": reaction, "status": status}
