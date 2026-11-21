"""Single-page reads: one kind of LinkedIn page, one navigation, one section.

Each kind names where its page lives and how the page is captured. The ones
that take a reference (a group, an event, a school, an article, a newsletter)
build their address from the segment the page's own URL carries, refused before
the browser starts when it is not one; the search kinds take keywords.

These addresses are LinkedIn's public routes, not measured markup: a kind that
lands somewhere else comes back with whatever that page says, which is why the
first live read of each one is checked by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, quote_plus

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.contracts import single_section_result
from linkedin_mcp_server.linkedin.identifiers import normalize_route_reference

PageKind = Literal[
    "saved_posts",
    "my_groups",
    "followers",
    "following",
    "group",
    "event",
    "school",
    "school_alumni",
    "article",
    "newsletter",
    "search_groups",
    "search_events",
    "search_schools",
]

_LINKEDIN = "https://www.linkedin.com"


@dataclass(frozen=True)
class PageTarget:
    """Where one read goes and what it is called in the result."""

    url: str
    section: str
    mode: CaptureMode


# The member's own lists, which take no reference.
_OWN_PAGES: dict[str, tuple[str, str]] = {
    "saved_posts": ("/my-items/saved-posts/", "saved_posts"),
    "my_groups": ("/groups/", "groups"),
    "followers": ("/mynetwork/network-manager/people-follow/followers/", "followers"),
    "following": ("/mynetwork/network-manager/people-follow/following/", "following"),
}

# Pages named by the segment after their route: (route, field, numeric, suffix,
# section, mode).
_REFERENCED_PAGES: dict[str, tuple[str, str, bool, str, str, CaptureMode]] = {
    "group": ("groups", "group_id", True, "/", "group", CaptureMode.ACTIVITY),
    "event": ("events", "event", False, "/", "event", CaptureMode.STANDARD),
    "school": ("school", "school", False, "/", "school", CaptureMode.STANDARD),
    "school_alumni": (
        "school",
        "school",
        False,
        "/people/",
        "alumni",
        CaptureMode.COMPANY_PEOPLE,
    ),
    "article": ("pulse", "article", False, "/", "article", CaptureMode.STANDARD),
    "newsletter": (
        "newsletters",
        "newsletter",
        False,
        "/",
        "newsletter",
        CaptureMode.STANDARD,
    ),
}

# LinkedIn's search verticals, all read as one results page.
_SEARCH_VERTICALS = {
    "search_groups": "groups",
    "search_events": "events",
    "search_schools": "schools",
}

#: Every kind, for validating a caller's choice.
PAGE_KINDS: tuple[str, ...] = (
    *_OWN_PAGES,
    *_REFERENCED_PAGES,
    *_SEARCH_VERTICALS,
)


def page_target(kind: str, reference: str | None = None) -> PageTarget:
    """Resolve a page kind and its reference to the one address it reads.

    Raises:
        InvalidReferenceError: when the kind is unknown, a reference is missing
            or given where none is taken, or the reference names no page of
            that kind.
    """
    if kind in _OWN_PAGES:
        if reference:
            raise InvalidReferenceError(f"{kind} takes no reference.")
        path, section = _OWN_PAGES[kind]
        return PageTarget(f"{_LINKEDIN}{path}", section, CaptureMode.ACTIVITY)
    if kind in _REFERENCED_PAGES:
        route, field, numeric, suffix, section, mode = _REFERENCED_PAGES[kind]
        if not reference or not reference.strip():
            raise InvalidReferenceError(f"Missing {field}.")
        segment = normalize_route_reference(
            reference,
            route=route,
            field=field,
            numeric=numeric,
            slug=route == "school",
        )
        url = f"{_LINKEDIN}/{route}/{quote(segment, safe='')}{suffix}"
        return PageTarget(url, section, mode)
    if kind in _SEARCH_VERTICALS:
        keywords = (reference or "").strip()
        if not keywords:
            raise InvalidReferenceError("Missing keywords.")
        vertical = _SEARCH_VERTICALS[kind]
        url = f"{_LINKEDIN}/search/results/{vertical}/?keywords={quote_plus(keywords)}"
        return PageTarget(url, "search_results", CaptureMode.SEARCH_RESULTS)
    raise InvalidReferenceError(
        f"Unknown page kind {kind!r}. Valid: {', '.join(PAGE_KINDS)}"
    )


class PageReader:
    """Read one page of a known kind into one section."""

    def __init__(self, capture: SectionCapture):
        self._capture = capture

    async def read_page(
        self,
        kind: PageKind,
        reference: str | None = None,
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """Navigate to the page of ``kind`` and return its text and references.

        Returns:
            {url, sections: {<section>: text}} plus optional ``references`` and
            ``section_errors``, as every single-page read does.
        """
        target = page_target(kind, reference)
        extracted = await self._capture.capture(
            target.url,
            section_name=target.section,
            plan=CapturePlan(target.mode, max_scrolls),
        )
        return single_section_result(target.url, target.section, extracted)
