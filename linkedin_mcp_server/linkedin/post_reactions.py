"""Read who reacted to one post, and how.

Measured 2026-11-22 on a `/feed/update/<urn>/` page, English locale, read
only:

- The post's reactors open in a dialog from the button LinkedIn marks with
  `data-jump-link-target` naming its reactors ("see N more reactions"), an
  attribute and not a word.
- The dialog (`role="dialog"`) holds one tab per reaction (`role="tab"` with
  `data-js-reaction-tab`, `ALL` first), each showing its count, then one row per
  reactor: a profile link (`a[href*="/in/"]`) beside an icon whose class
  carries LinkedIn's own reaction type (`data-test-reactions-icon-type-LIKE`,
  the codes the reaction tools confirm with).
- Ten rows load at first. Ten more load either when the dialog's own scroll
  container reaches its end (measured headless, 1280 px wide) or from a "show
  more results" button below the last row (measured headed on a 945 px wide
  window). The button is told by its place, after the last reactor and
  neither a tab nor the dialog's close button, never by its words.

So the reaction of each reactor is read from its icon's type code, and no
label is read. Opening the dialog changes nothing about the post.

Verified live 2026-11-22 through the MCP tool, headed: 30 reactors over two
pages of the button, each with a clean name and its reaction, and the counts
of all 50.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin.identifiers import (
    normalize_post_reference,
    post_page_url,
)
from linkedin_mcp_server.linkedin.link_metadata import classify_link
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_actions import REACTION_TYPES
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

SECTION = "reactions"
#: Each reaction's name as react_to_post takes it, by LinkedIn's type code.
REACTION_NAMES: dict[str, str] = {code: name for name, code in REACTION_TYPES.items()}

_BUTTON_SELECTOR = 'button[data-jump-link-target*="reactors"]'
_POST_READY_TIMEOUT = 10.0
_DIALOG_TIMEOUT = 6.0
#: How long one scroll may take to load its next rows: measured about three
#: seconds live on 2026-11-22.
_SCROLL_WAIT = 6.0
#: How long the dialog settles after its first rows before the first scroll.
_SETTLE = 2.0
_POLL = 0.25
DEFAULT_SCROLLS = 10

# The open reactors dialog: its tabs' type codes and counts, its rows' profile
# links with their reaction codes, and its text.
REACTORS_JS = r"""() => {
    const dialog = [...document.querySelectorAll('[role="dialog"]')]
        .find((d) => d.querySelector('[role="tab"][data-js-reaction-tab]'));
    if (!dialog) return null;
    const codeOf = (root) => {
        for (const el of root.querySelectorAll('*')) {
            const code = (el.getAttribute('class') || '')
                .match(/data-test-reactions-icon-type-([A-Z_]+)/);
            if (code) return code[1];
        }
        return null;
    };
    const tabs = [...dialog.querySelectorAll('[role="tab"][data-js-reaction-tab]')]
        .map((tab) => ({
            code: tab.getAttribute('data-js-reaction-tab'),
            text: (tab.innerText || '').replace(/\s+/g, ' ').trim(),
        }));
    const pathOf = (a) => {
        try { return new URL(a.href).pathname; } catch (error) { return ''; }
    };
    const rows = [];
    const seen = new Set();
    for (const link of dialog.querySelectorAll('a[href*="/in/"]')) {
        const path = pathOf(link);
        if (!path || seen.has(path)) continue;
        // The row: the widest ancestor that still links to this one person.
        let row = link;
        for (let node = link.parentElement; node && node !== dialog; node = node.parentElement) {
            const people = new Set([...node.querySelectorAll('a[href*="/in/"]')].map(pathOf));
            if (people.size !== 1) break;
            row = node;
        }
        seen.add(path);
        // The name is the row's first line, whatever the words after it.
        const name = (row.innerText || '').split('\n').map((line) => line.trim())
            .find(Boolean) || '';
        rows.push({href: link.href, name: name.slice(0, 120), code: codeOf(row)});
    }
    return {tabs, rows, text: dialog.innerText || ''};
}"""

# Scrolls the dialog's scroll containers to their end; false when it has none.
SCROLL_JS = r"""(back) => {
    const dialog = [...document.querySelectorAll('[role="dialog"]')]
        .find((d) => d.querySelector('[role="tab"][data-js-reaction-tab]'));
    if (!dialog) return false;
    // Every container that really scrolls and holds reactors, as measured:
    // which one loads the next rows is LinkedIn's to decide.
    // The list that really scrolls (overflow auto or scroll, measured auto)
    // and holds reactors. Visually hidden labels clip their text too, and are
    // taller than their box, so overflow alone would take them for the list.
    const scrollers = [...dialog.querySelectorAll('*')].filter((el) =>
        el.scrollHeight > el.clientHeight + 20
        && ['auto', 'scroll'].includes(getComputedStyle(el).overflowY)
        && el.querySelector('a[href*="/in/"]'));
    for (const old of document.querySelectorAll('[data-mcp-reactors-list]')) {
        old.removeAttribute('data-mcp-reactors-list');
    }
    if (!scrollers.length) return false;
    // The innermost one is the list itself; it is marked for the wheel.
    const list = scrollers.filter((el) => !scrollers.some((o) => o !== el && el.contains(o)))[0];
    list.setAttribute('data-mcp-reactors-list', '');
    // ``back`` moves up from the end first, so reaching it again is a new
    // scroll for whatever watches it.
    list.scrollTop = back
        ? Math.max(0, list.scrollHeight - list.clientHeight - 400)
        : list.scrollHeight;
    return true;
}"""


# Marks the button below the last reactor that loads more: in the dialog,
# after its last profile link, neither a tab nor the close button.
MORE_BUTTON_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const dialog = [...document.querySelectorAll('[role="dialog"]')]
        .find((d) => d.querySelector('[role="tab"][data-js-reaction-tab]'));
    if (!dialog) return false;
    const links = dialog.querySelectorAll('a[href*="/in/"]');
    const last = links[links.length - 1];
    if (!last) return false;
    const after = [...dialog.querySelectorAll('button')].filter((button) =>
        button.getAttribute('role') !== 'tab'
        && !button.closest('[role="tab"]')
        && !button.hasAttribute('data-test-modal-close-btn')
        && !button.contains(last)
        && (last.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING)
        && button.getClientRects().length > 0);
    if (after.length !== 1) return false;
    after[0].setAttribute(mark, '');
    return true;
}"""

_MORE_MARK = "data-mcp-reactors-more"


def _count(text: str) -> int | None:
    """The count a tab shows, its digits read whatever their grouping."""
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


class PostReactionsReader:
    """Read the reactors of a post from its reactors dialog."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def read_post_reactions(
        self, post: str, max_scrolls: int | None = None
    ) -> dict[str, Any]:
        """Open the post's reactors and read each one with their reaction.

        Returns:
            {url, sections: {reactions: text}, references: {reactions: [person
            references, each with its reaction as context]}, reaction_counts:
            {total, <reaction>: n}}. A post that shows no reactors button comes
            back as a section error.
        """
        path = normalize_post_reference(post)
        url = post_page_url(path)
        page = self._session.page
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        button = page.locator(_BUTTON_SELECTOR)
        deadline = self._session.monotonic() + _POST_READY_TIMEOUT
        while await button.count() != 1:
            if self._session.monotonic() >= deadline:
                return {
                    "url": url,
                    "sections": {},
                    "section_errors": {
                        SECTION: {
                            "error_type": "no_reactors_list",
                            "error_message": (
                                f"{url} shows no single reactors button, so its "
                                "reactors could not be listed. A post with no "
                                "reactions, or only a few, has none."
                            ),
                        }
                    },
                }
            await self._session.delay(_POLL)
        async with linkedin_element(button, timeout=5000) as element:
            await element.click(timeout=5000)
        deadline = self._session.monotonic() + _DIALOG_TIMEOUT
        found = await page.evaluate(REACTORS_JS)
        while not (found and found["rows"]) and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await page.evaluate(REACTORS_JS)
        if not found:
            raise LinkedInOperationError(
                f"The reactors button of {url} was clicked, but no reactors "
                "dialog with reaction tabs opened."
            )
        scrolls = DEFAULT_SCROLLS if max_scrolls is None else max_scrolls
        if scrolls:
            await self._session.delay(_SETTLE)
        for attempt in range(scrolls):
            if await page.evaluate(MORE_BUTTON_JS, _MORE_MARK):
                # The dialog pages with a button rather than on scroll.
                async with linkedin_element(
                    page.locator(f"button[{_MORE_MARK}]"), timeout=3000
                ) as element:
                    await element.click(timeout=3000)
                more = await self._more_rows(len(found["rows"]))
            elif await self._scroll():
                more = await self._more_rows(len(found["rows"]))
            else:
                more = None
            logger.debug(
                "Reactors page %d: %d rows, then %s",
                attempt + 1,
                len(found["rows"]),
                "no more" if more is None else len(more["rows"]),
            )
            if more is None:
                break
            found = more
        await page.keyboard.press("Escape")
        return _result(url, found)

    async def _scroll(self) -> bool:
        """Scroll the dialog's list, waiting for it to become scrollable.

        Measured live: right after its first rows render, the list is not yet
        taller than its box, so a scroll at once finds nothing to scroll.
        """
        page = self._session.page
        deadline = self._session.monotonic() + _SCROLL_WAIT
        while not await page.evaluate(SCROLL_JS, False):
            if self._session.monotonic() >= deadline:
                return False
            await self._session.delay(_POLL * 2)
        return True

    async def _more_rows(self, had: int) -> dict[str, Any] | None:
        """The dialog once a scroll has loaded more rows, or None by the wait."""
        deadline = self._session.monotonic() + _SCROLL_WAIT
        while self._session.monotonic() < deadline:
            await self._session.delay(_POLL * 2)
            more = await self._session.page.evaluate(REACTORS_JS)
            if more and len(more["rows"]) > had:
                return more
        return None


def _result(url: str, found: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for tab in found["tabs"]:
        count = _count(tab["text"])
        if count is None:
            continue
        code = tab["code"]
        if code == "ALL":
            counts["total"] = count
        else:
            counts[REACTION_NAMES.get(code, code.lower())] = count
    references: list[dict[str, Any]] = []
    for row in found["rows"]:
        classified = classify_link(row["href"])
        if classified is None or classified[0] != "person":
            continue
        reference: dict[str, Any] = {"kind": "person", "url": classified[1]}
        name = (row.get("name") or "").strip()
        if name:
            reference["text"] = name
        if row["code"]:
            reference["context"] = REACTION_NAMES.get(row["code"], row["code"].lower())
        references.append(reference)
    result: dict[str, Any] = {
        "url": url,
        "sections": {SECTION: found["text"]},
        "reaction_counts": counts,
    }
    if references:
        result["references"] = {SECTION: references}
    return result
