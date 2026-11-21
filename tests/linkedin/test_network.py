"""Tests for the readers of the signed-in member's network pages."""

from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest

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
from linkedin_mcp_server.linkedin.network import NetworkReader
from linkedin_mcp_server.linkedin.session import PageSession


def _reader(page) -> NetworkReader:
    session = PageSession(page)
    navigator = PageNavigator(session)
    return NetworkReader(SectionCapture(session, navigator, PageContentReader(session)))


_READS = [
    (
        "get_invitations",
        {},
        "https://www.linkedin.com/mynetwork/invitation-manager/received/",
        "invitations",
    ),
    (
        "get_invitations",
        {"direction": "sent"},
        "https://www.linkedin.com/mynetwork/invitation-manager/sent/",
        "invitations",
    ),
    (
        "get_connections",
        {},
        "https://www.linkedin.com/mynetwork/invite-connect/connections/",
        "connections",
    ),
    (
        "get_notifications",
        {},
        "https://www.linkedin.com/notifications/",
        "notifications",
    ),
    (
        "get_profile_views",
        {},
        "https://www.linkedin.com/me/profile-views/",
        "profile_views",
    ),
]


@pytest.mark.parametrize(("method", "kwargs", "url", "section"), _READS)
async def test_each_read_is_one_capture_of_its_own_page(
    mock_page, method, kwargs, url, section
):
    reader = _reader(mock_page)
    refs: list[Reference] = [{"kind": "person", "url": "/in/ada/", "context": "x"}]
    with patch.object(
        reader._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text="Ada Lovelace", references=refs),
    ) as capture:
        result = await getattr(reader, method)(**kwargs, max_scrolls=3)

    assert capture.await_args_list == [
        call(url, section_name=section, plan=CapturePlan(CaptureMode.ACTIVITY, 3))
    ]
    assert result == {
        "url": url,
        "sections": {section: "Ada Lovelace"},
        "references": {section: refs},
    }


@pytest.mark.parametrize(("method", "kwargs", "url", "section"), _READS)
async def test_a_rate_limited_page_is_a_section_error(
    mock_page, method, kwargs, url, section
):
    reader = _reader(mock_page)
    with patch.object(
        reader._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[]),
    ):
        result = await getattr(reader, method)(**kwargs)

    assert result == {
        "url": url,
        "sections": {},
        "section_errors": {section: rate_limited_section_error()},
    }


async def test_a_failed_capture_keeps_its_diagnostic(mock_page):
    reader = _reader(mock_page)
    error = {"error_type": "TimeoutError", "error_message": "slow"}
    with patch.object(
        reader._capture,
        "capture",
        new_callable=AsyncMock,
        return_value=ExtractedSection(text="", references=[], error=error),
    ):
        result = await reader.get_connections()

    assert result["section_errors"] == {"connections": error}


async def test_an_unknown_invitation_direction_never_navigates(mock_page):
    reader = _reader(mock_page)
    with patch.object(reader._capture, "capture", new_callable=AsyncMock) as capture:
        with pytest.raises(ValueError, match="direction"):
            await reader.get_invitations("archived")  # ty: ignore[invalid-argument-type]
    capture.assert_not_awaited()
