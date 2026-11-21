"""Tests for the single-page reads: one kind, one navigation, one section."""

from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
    rate_limited_section_error,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.pages import PAGE_KINDS, PageReader, page_target
from linkedin_mcp_server.linkedin.session import PageSession

L = "https://www.linkedin.com"

#: Every kind with a reference it accepts, and the one address it must read.
READS = [
    (
        "saved_posts",
        None,
        f"{L}/my-items/saved-posts/",
        "saved_posts",
        CaptureMode.ACTIVITY,
    ),
    ("my_groups", None, f"{L}/groups/", "groups", CaptureMode.ACTIVITY),
    (
        "followers",
        None,
        f"{L}/mynetwork/network-manager/people-follow/followers/",
        "followers",
        CaptureMode.ACTIVITY,
    ),
    (
        "following",
        None,
        f"{L}/mynetwork/network-manager/people-follow/following/",
        "following",
        CaptureMode.ACTIVITY,
    ),
    (
        "my_events",
        None,
        f"{L}/mynetwork/network-manager/events/",
        "events",
        CaptureMode.ACTIVITY,
    ),
    (
        "my_newsletters",
        None,
        f"{L}/mynetwork/network-manager/newsletters/",
        "newsletters",
        CaptureMode.ACTIVITY,
    ),
    (
        "followed_pages",
        None,
        f"{L}/mynetwork/network-manager/company/",
        "followed_pages",
        CaptureMode.ACTIVITY,
    ),
    (
        "catch_up",
        None,
        f"{L}/mynetwork/catch-up/all/",
        "catch_up",
        CaptureMode.ACTIVITY | CaptureMode.RAIL_FIRST,
    ),
    (
        "people_you_may_know",
        None,
        f"{L}/mynetwork/grow/",
        "people_you_may_know",
        CaptureMode.ACTIVITY | CaptureMode.RAIL_FIRST,
    ),
    (
        "dashboard",
        None,
        f"{L}/dashboard/",
        "dashboard",
        CaptureMode.ACTIVITY,
    ),
    (
        "search_appearances",
        None,
        f"{L}/analytics/search-appearances/",
        "search_appearances",
        CaptureMode.ACTIVITY,
    ),
    (
        "content_analytics",
        None,
        f"{L}/analytics/creator/content/",
        "content_analytics",
        CaptureMode.ACTIVITY,
    ),
    (
        "group",
        f"{L}/groups/1234567/",
        f"{L}/groups/1234567/",
        "group",
        CaptureMode.ACTIVITY,
    ),
    (
        "event",
        "7262891234567890123",
        f"{L}/events/7262891234567890123/",
        "event",
        CaptureMode.STANDARD,
    ),
    (
        "school",
        f"{L}/school/uc-berkeley/",
        f"{L}/school/uc-berkeley/",
        "school",
        CaptureMode.STANDARD,
    ),
    (
        "school_alumni",
        "uc-berkeley",
        f"{L}/school/uc-berkeley/people/",
        "alumni",
        CaptureMode.COMPANY_PEOPLE,
    ),
    (
        "article",
        f"{L}/pulse/engines-ada/",
        f"{L}/pulse/engines-ada/",
        "article",
        CaptureMode.STANDARD,
    ),
    (
        "newsletter",
        "ai-weekly-7012345678901234567",
        f"{L}/newsletters/ai-weekly-7012345678901234567/",
        "newsletter",
        CaptureMode.STANDARD,
    ),
    (
        "search_groups",
        "data science",
        f"{L}/search/results/groups/?keywords=data+science",
        "search_results",
        CaptureMode.SEARCH_RESULTS,
    ),
    (
        "search_events",
        "AI",
        f"{L}/search/results/events/?keywords=AI",
        "search_results",
        CaptureMode.SEARCH_RESULTS,
    ),
    (
        "search_schools",
        "Berkeley",
        f"{L}/search/results/schools/?keywords=Berkeley",
        "search_results",
        CaptureMode.SEARCH_RESULTS,
    ),
]


def _reader(page, landed: str | None = None) -> PageReader:
    """A reader whose page, once the capture is done, is at *landed*."""
    if landed is not None:
        page.url = landed
    session = PageSession(page)
    return PageReader(
        SectionCapture(session, PageNavigator(session), PageContentReader(session)),
        session,
    )


def test_every_kind_is_covered_here():
    assert sorted(kind for kind, *_ in READS) == sorted(PAGE_KINDS)


@pytest.mark.parametrize(("kind", "reference", "url", "section", "mode"), READS)
async def test_each_kind_is_one_capture_of_its_own_page(
    mock_page, kind, reference, url, section, mode
):
    reader = _reader(mock_page, landed=url)
    refs: list[Reference] = [{"kind": "person", "url": "/in/ada/", "context": "x"}]
    with patch.object(
        reader._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text="Ada", references=refs),
    ) as capture:
        result = await reader.read_page(kind, reference, max_scrolls=2)

    assert capture.await_args_list == [
        call(url, section_name=section, plan=CapturePlan(mode, 2))
    ]
    assert result == {
        "url": url,
        "sections": {section: "Ada"},
        "references": {section: refs},
    }


async def test_a_rate_limited_page_is_a_section_error(mock_page):
    reader = _reader(mock_page, landed=f"{L}/my-items/saved-posts/")
    with patch.object(
        reader._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[]),
    ):
        result = await reader.read_page("saved_posts")

    assert result["sections"] == {}
    assert result["section_errors"] == {"saved_posts": rate_limited_section_error()}


@pytest.mark.parametrize(
    ("kind", "reference"),
    [
        ("group", "abc"),
        ("group", f"{L}/in/ada/"),
        ("group", None),
        ("event", "../feed"),
        ("event", "a/b"),
        ("school", "a b"),
        ("school", f"{L}/company/acme/"),
        ("article", f"{L}/pulse/../in/ada"),
        ("newsletter", ""),
        ("search_groups", "   "),
        ("saved_posts", "x"),
        ("not_a_kind", None),
    ],
)
async def test_a_reference_that_names_no_page_never_navigates(
    mock_page, kind, reference
):
    reader = _reader(mock_page)
    with patch.object(reader._capture, "capture", new_callable=AsyncMock) as capture:
        with pytest.raises(InvalidReferenceError):
            await reader.read_page(kind, reference)
    capture.assert_not_awaited()


def test_keywords_are_escaped_into_one_parameter():
    target = page_target("search_events", "AI & ML?x=1")
    assert target.url == f"{L}/search/results/events/?keywords=AI+%26+ML%3Fx%3D1"


@pytest.mark.parametrize(
    "landed",
    [f"{L}/feed/", f"{L}/authwall?trk=x", f"{L}/in/ada/"],
)
async def test_a_page_answered_with_another_part_of_the_site_returns_no_text(
    mock_page, landed
):
    # LinkedIn sends a route the account lacks to the feed or elsewhere; that
    # page's text must not come back under the asked section's name.
    reader = _reader(mock_page, landed=landed)
    with patch.object(
        reader._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text="Start a post", references=[]),
    ):
        result = await reader.read_page("dashboard")

    assert result["sections"] == {}
    error = result["section_errors"]["dashboard"]
    assert error["error_type"] == "page_redirected"
    assert "/dashboard/" in error["error_message"]


@pytest.mark.parametrize(
    ("kind", "reference", "landed"),
    [
        ("my_events", None, f"{L}/mynetwork/grow/"),
        ("followed_pages", None, f"{L}/mynetwork/"),
        ("content_analytics", None, f"{L}/analytics/"),
        ("search_schools", "Berkeley", f"{L}/search/results/all/?keywords=Berkeley"),
        # A shared prefix is not a shared page: the match stops at a segment.
        ("search_schools", "Berkeley", f"{L}/search/results/schoolsx/"),
    ],
)
async def test_a_sibling_page_in_the_same_part_of_the_site_returns_no_text(
    mock_page, kind, reference, landed
):
    # The likeliest fallback for a page the account lacks is its neighbour,
    # whose text must not come back under the asked section's name.
    reader = _reader(mock_page, landed=landed)
    with patch.object(
        reader._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text="Ada", references=[]),
    ):
        result = await reader.read_page(kind, reference)

    section = page_target(kind, reference).section
    assert result["sections"] == {}
    assert result["section_errors"][section]["error_type"] == "page_redirected"


@pytest.mark.parametrize(
    ("kind", "landed", "section"),
    [
        ("my_events", f"{L}/mynetwork/network-manager/events/attending/", "events"),
        ("dashboard", f"{L}/dashboard?trk=nav", "dashboard"),
    ],
)
async def test_a_landing_beneath_an_own_page_is_kept(mock_page, kind, landed, section):
    # A sub-tab, a dropped trailing slash or a tracking query is the same page.
    reader = _reader(mock_page, landed=landed)
    with patch.object(
        reader._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text="Ada", references=[]),
    ):
        result = await reader.read_page(kind)

    assert result["sections"] == {section: "Ada"}


async def test_a_deeper_landing_in_the_same_part_of_the_site_is_kept(mock_page):
    # A canonical slug or a sub-tab is the same page, not a redirect.
    reader = _reader(mock_page, landed=f"{L}/pulse/engines-ada-lovelace-1a2b/")
    with patch.object(
        reader._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text="Ada", references=[]),
    ):
        result = await reader.read_page("article", "engines-ada")

    assert result["sections"] == {"article": "Ada"}
