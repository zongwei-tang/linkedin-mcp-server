"""Single-page reads: one kind of LinkedIn page, one navigation, one section.

Each kind names where its page lives and how the page is captured. The ones
that take a reference (a group, an event, a school, an article, a newsletter)
build their address from the segment the page's own URL carries, refused before
the browser starts when it is not one; the search kinds take keywords.

These addresses are LinkedIn's public routes, not measured markup. A read that
LinkedIn sends to another page (the feed, a sign-in wall, a sibling page in the
same part of the site when the account cannot see the one asked for) returns a
``page_redirected`` section error instead of that other page's text, judged by
the path it landed on: the page's own path or beneath it, or, for a page named
by its slug, the part of the site. The first live read of each kind is still
checked by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, quote_plus, urlparse

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    single_section_result,
)
from linkedin_mcp_server.linkedin.identifiers import normalize_route_reference
from linkedin_mcp_server.linkedin.session import PageSession

PageKind = Literal[
    "saved_posts",
    "my_groups",
    "followers",
    "following",
    "my_events",
    "my_newsletters",
    "followed_pages",
    "catch_up",
    "people_you_may_know",
    "dashboard",
    "search_appearances",
    "content_analytics",
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
    "my_events": ("/mynetwork/network-manager/events/", "events"),
    "my_newsletters": ("/mynetwork/network-manager/newsletters/", "newsletters"),
    "followed_pages": ("/mynetwork/network-manager/company/", "followed_pages"),
    "catch_up": ("/mynetwork/catch-up/all/", "catch_up"),
    "people_you_may_know": ("/mynetwork/grow/", "people_you_may_know"),
    "dashboard": ("/dashboard/", "dashboard"),
    "search_appearances": ("/analytics/search-appearances/", "search_appearances"),
    "content_analytics": ("/analytics/creator/content/", "content_analytics"),
}

# Own pages read otherwise than a plain activity list. Measured 2026-11-21:
# these two put the "Manage my network" rail and the footer first.
_OWN_PAGE_MODES: dict[str, CaptureMode] = {
    "catch_up": CaptureMode.ACTIVITY | CaptureMode.RAIL_FIRST,
    "people_you_may_know": CaptureMode.ACTIVITY | CaptureMode.RAIL_FIRST,
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
        mode = _OWN_PAGE_MODES.get(kind, CaptureMode.ACTIVITY)
        return PageTarget(f"{_LINKEDIN}{path}", section, mode)
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


def _landed_within(landed: str, target: str, *, area_only: bool) -> bool:
    """Whether *landed* is *target*'s page or beneath it, query aside.

    A page named by its slug is judged by its part of the site alone, because
    LinkedIn canonicalises the slug (an article gains its id). Every other page
    has a fixed path, and a sibling of it in the same part of the site (My
    Network's Grow page for its events) is another page.
    """
    landed_path = urlparse(landed).path.rstrip("/")
    target_path = urlparse(target).path.rstrip("/")
    if area_only:
        target_path = "/" + target_path.strip("/").split("/", 1)[0]
    # The "/" keeps the match on a segment boundary: /schoolsx is not /schools.
    return landed_path == target_path or landed_path.startswith(target_path + "/")


def redirected_section_error(target: str, landed: str) -> dict[str, str]:
    """The section error for a read LinkedIn answered with another page."""
    return {
        "error_type": "page_redirected",
        "error_message": (
            f"LinkedIn showed {urlparse(landed).path or landed} instead of "
            f"{urlparse(target).path}, so that page's text is not returned. The "
            "account may not have this page, or LinkedIn moved it."
        ),
    }


class PageReader:
    """Read one page of a known kind into one section."""

    def __init__(self, capture: SectionCapture, session: PageSession):
        self._capture = capture
        self._session = session

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
        # Only text needs the check: a failed read already says why it failed.
        landed = self._session.page.url
        if (
            extracted.text
            and extracted.text != RATE_LIMITED_SECTION_TEXT
            and not _landed_within(
                landed, target.url, area_only=kind in _REFERENCED_PAGES
            )
        ):
            return {
                "url": target.url,
                "sections": {},
                "section_errors": {
                    target.section: redirected_section_error(target.url, landed)
                },
            }
        return single_section_result(target.url, target.section, extracted)
