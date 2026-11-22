"""Write actions on one messaging thread: star and unstar, mute and unmute.

Measured 2026-11-22 on an inbox thread that was already read, by choosing each
item from the thread's options menu, reloading, and choosing the item that took
its place:

- LinkedIn's own conversation data (the `messengerConversations` query the page
  loads, the same data archive_conversation reads the folder from) says the
  state. A starred thread's `categories` gain `STARRED`; a muted thread's
  `notificationStatus` is `MUTE`, and `ACTIVE` otherwise.
- The options menu offers Star or Remove star, and Mute or Unmute. Its items
  carry no icon, so they are found by their whole visible text in the
  per-locale table (`ConversationActionTextTable`), English only for now: in
  another locale the item is not found and nothing is clicked.

So the state is read from the data before anything is clicked, and again
after a reload, which must show the change. Opening a thread by its URL marks
it read, as archive_conversation already does.

Verified live 2026-11-22 through the MCP tools on one read inbox thread,
across three server processes: star and mute, then already_starred and
already_muted, unstar and unmute, then already_unstarred and already_unmuted.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal
from urllib.parse import unquote

import anyio

from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin.conversations import (
    _CONVERSATION_TYPE,
    _THREAD_MENU_PRELUDE_JS,
    _THREAD_MENU_TIMEOUT,
    _THREAD_POLL,
    _THREAD_READY_TIMEOUT,
    THREAD_MENU_SURVEY_JS,
    THREAD_OPTIONS_JS,
    _on_thread,
    is_conversations_payload_response,
)
from linkedin_mcp_server.linkedin.identifiers import (
    messaging_thread_url,
    normalize_thread_id,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession
from linkedin_mcp_server.linkedin.text import (
    CONVERSATION_ACTIONS_EN_US,
    ConversationActionTextTable,
)

logger = logging.getLogger(__name__)

ThreadFlag = Literal["starred", "muted"]

_OPTIONS_MARK = "data-mcp-flag-options"
_SEEN_MARK = "data-mcp-flag-seen"
_ITEM_MARK = "data-mcp-flag-item"
_STARRED_CATEGORY = "STARRED"
_MUTED_STATUS = "MUTE"

# The menu's items whose whole text is ``word``. Marked for the click only when
# it is the one match.
THREAD_MENU_ITEM_JS = (
    r"""(opts) => {
    const {word, trigger, seen, mark} = opts;
    const after = true;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }"""
    + _THREAD_MENU_PRELUDE_JS
    + r"""
    const hits = menuItems.filter(el => squash(el.innerText) === word);
    if (hits.length === 1) hits[0].setAttribute(mark, '');
    return {hits: hits.length};
}"""
)


def thread_state(payload: Any, thread_key: str) -> dict[str, Any] | None:
    """The categories and notification status ``payload`` gives one thread.

    The conversation is the `Conversation` whose `backendUrn` names exactly
    that thread. None when the payload holds none, or holds it without a list
    of category strings.
    """
    urn = f"urn:li:messagingThread:{thread_key}"
    found: dict[str, Any] | None = None
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        categories = node.get("categories")
        if (
            node.get("_type") == _CONVERSATION_TYPE
            and node.get("backendUrn") == urn
            and isinstance(categories, list)
            and all(isinstance(category, str) for category in categories)
        ):
            status = node.get("notificationStatus")
            found = {
                "categories": list(categories),
                "notificationStatus": status if isinstance(status, str) else None,
            }
        stack.extend(node.values())
    return found


def flag_of(state: dict[str, Any], flag: ThreadFlag) -> bool | None:
    """Whether ``state`` shows the thread starred or muted; None when unsaid."""
    if flag == "starred":
        return _STARRED_CATEGORY in state["categories"]
    status = state["notificationStatus"]
    return None if status is None else status == _MUTED_STATUS


class _ThreadStateListener:
    """Read one thread's state from the conversation payloads a load fetches.

    Installed before the navigation, because the open thread's conversation
    arrives with the page's first queries. Keeps the state of the last payload
    that named the thread.
    """

    _READ_DRAIN_TIMEOUT = 2.0
    _CANCEL_DRAIN_TIMEOUT = 1.0

    def __init__(self, page: Any, thread_key: str):
        self._page = page
        self._thread_key = thread_key
        self._pending: list[asyncio.Task[None]] = []
        self.state: dict[str, Any] | None = None

    def install(self) -> None:
        self._page.on("response", self._handle_response)

    def remove(self) -> None:
        try:
            self._page.remove_listener("response", self._handle_response)
        except Exception:
            logger.debug("Failed to remove thread state listener", exc_info=True)

    def _handle_response(self, response: Any) -> None:
        if not is_conversations_payload_response(response.url):
            return

        async def _read() -> None:
            try:
                payload = json.loads(await response.body())
            except Exception:
                return
            state = thread_state(payload, self._thread_key)
            if state is not None:
                self.state = state

        self._pending.append(asyncio.create_task(_read()))

    async def drain(self) -> None:
        pending = self._pending
        if not pending:
            return
        try:
            await asyncio.wait(pending, timeout=self._READ_DRAIN_TIMEOUT)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.wait(pending, timeout=self._CANCEL_DRAIN_TIMEOUT)
            pending.clear()


class ConversationFlagActions:
    """Own starring and muting a messaging thread for the signed-in member."""

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        action_text: ConversationActionTextTable = CONVERSATION_ACTIONS_EN_US,
    ):
        self._session = session
        self._navigator = navigator
        self._text = action_text

    async def set_thread_flag(
        self, thread_id: str, flag: ThreadFlag, value: bool
    ) -> dict[str, Any]:
        """Star, unstar, mute or unmute one thread, leaving it as asked.

        Returns:
            {url, thread_id, <flag>: value, status} where status is the change
            made ("starred", "unstarred", "muted", "unmuted") or the same with
            "already_" in front.
        """
        thread_key = unquote(normalize_thread_id(thread_id))
        url = messaging_thread_url(thread_key, "/")
        verb = _STATUS[flag][value]
        before = await self._read_state(url, thread_key)
        current = None if before is None else flag_of(before, flag)
        if current is None:
            raise LinkedInOperationError(
                f"Thread {thread_key}'s conversation data did not say whether it "
                f"is {flag}, so nothing was clicked."
            )
        if current == value:
            return _result(url, thread_key, flag, value, f"already_{verb}")

        await self._choose(url, thread_key, self._item_word(flag, value))
        await self._session.delay(NAV_DELAY)
        after = await self._read_state(url, thread_key)
        shown = None if after is None else flag_of(after, flag)
        if shown != value:
            seen = "nothing about it" if shown is None else f"it still {flag}={shown}"
            raise ActionOutcomeError(
                f"The menu item to make thread {thread_key} {flag}={value} was "
                f"chosen, but after a reload LinkedIn's data showed {seen}, so "
                "it may not have kept it. Calling this again is safe: it reads "
                "the state first."
            )
        return _result(url, thread_key, flag, value, verb)

    def _item_word(self, flag: ThreadFlag, value: bool) -> str:
        text = self._text
        if flag == "starred":
            return text.star_item if value else text.unstar_item
        return text.mute_item if value else text.unmute_item

    async def _read_state(self, url: str, thread_key: str) -> dict[str, Any] | None:
        """Open the thread and read its state from the conversation data."""
        page = self._session.page
        listener = _ThreadStateListener(page, thread_key)
        listener.install()
        try:
            await self._navigator._navigate_to_page(url)
            await self._session.check_rate_limit()
            if not _on_thread(page.url, thread_key):
                await self._navigator._raise_if_auth_barrier(page.url)
                raise LinkedInOperationError(
                    f"Thread {thread_key} could not be opened: the navigation "
                    f"ended on {page.url}."
                )
            await self._session.dismiss_modal()
            deadline = self._session.monotonic() + _THREAD_READY_TIMEOUT
            while listener.state is None and self._session.monotonic() < deadline:
                await self._session.delay(_THREAD_POLL)
            return listener.state
        finally:
            listener.remove()
            await listener.drain()

    async def _choose(self, url: str, thread_key: str, word: str) -> None:
        """Open the thread's options menu and click its item reading ``word``."""
        page = self._session.page
        deadline = self._session.monotonic() + _THREAD_READY_TIMEOUT
        while True:
            found = await page.evaluate(
                THREAD_OPTIONS_JS,
                {"prefix": self._text.options_prefix, "mark": _OPTIONS_MARK},
            )
            if isinstance(found, dict) and found.get("triggers") == 1:
                break
            if self._session.monotonic() >= deadline:
                raise LinkedInOperationError(
                    f"Thread {thread_key}'s options button could not be told "
                    "apart. Nothing was clicked."
                )
            await self._session.delay(_THREAD_POLL)
        await page.evaluate(
            THREAD_MENU_SURVEY_JS,
            {"after": False, "trigger": _OPTIONS_MARK, "seen": _SEEN_MARK},
        )
        await page.locator(f"main button[{_OPTIONS_MARK}]").click(timeout=5000)
        options = {
            "word": word,
            "trigger": _OPTIONS_MARK,
            "seen": _SEEN_MARK,
            "mark": _ITEM_MARK,
        }
        deadline = self._session.monotonic() + _THREAD_MENU_TIMEOUT
        menu = await page.evaluate(THREAD_MENU_ITEM_JS, options)
        while menu["hits"] != 1 and self._session.monotonic() < deadline:
            await self._session.delay(_THREAD_POLL)
            menu = await page.evaluate(THREAD_MENU_ITEM_JS, options)
        if menu["hits"] != 1:
            await page.keyboard.press("Escape")
            raise LinkedInOperationError(
                f"Thread {thread_key}'s options menu did not offer exactly one "
                f"{word!r} ({menu['hits']} found). It was closed; nothing was "
                "clicked."
            )
        if not _on_thread(page.url, thread_key):
            await page.keyboard.press("Escape")
            raise LinkedInOperationError(
                f"Thread {thread_key} was left for {page.url} before {word!r} "
                "could be chosen. Nothing was clicked."
            )
        await page.locator(f"[{_ITEM_MARK}]").click(timeout=5000)


_STATUS: dict[ThreadFlag, dict[bool, str]] = {
    "starred": {True: "starred", False: "unstarred"},
    "muted": {True: "muted", False: "unmuted"},
}


def _result(
    url: str, thread_key: str, flag: ThreadFlag, value: bool, status: str
) -> dict[str, Any]:
    return {"url": url, "thread_id": thread_key, flag: value, "status": status}
