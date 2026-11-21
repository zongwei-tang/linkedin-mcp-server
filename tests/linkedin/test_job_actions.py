"""Tests for the saved-list read that save_job and unsave_job judge by."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin.job_actions import (
    SAVED_PAGES_READ,
    saved_job_ids_from,
)


def _jobs(result: dict) -> MagicMock:
    jobs = MagicMock()
    jobs.get_saved_jobs = AsyncMock(return_value=result)
    return jobs


async def test_the_saved_stage_is_read_to_its_page_limit():
    jobs = _jobs({"url": "u", "sections": {"saved_jobs": "x"}, "job_ids": ["1", "2"]})

    assert await saved_job_ids_from(jobs)() == ["1", "2"]
    jobs.get_saved_jobs.assert_awaited_once_with(
        max_pages=SAVED_PAGES_READ, stage="saved"
    )


async def test_an_empty_list_is_an_answer():
    assert await saved_job_ids_from(_jobs({"url": "u", "sections": {}}))() == []


async def test_a_failed_read_is_never_taken_for_an_empty_list():
    # Taken for empty, it would make every saved posting look unsaved.
    jobs = _jobs(
        {
            "url": "u",
            "sections": {},
            "section_errors": {"saved_jobs": {"error_type": "x"}},
        }
    )

    with pytest.raises(LinkedInOperationError, match="could not be read"):
        await saved_job_ids_from(jobs)()


async def test_a_list_that_ends_where_linkedin_stops_paging_is_read():
    # Every list longer than one page ends this way, because the tracker drops
    # the offset; refusing it refused every account with saved jobs.
    jobs = _jobs(
        {
            "url": "u",
            "sections": {"saved_jobs": "x"},
            "job_ids": ["1"],
            "section_errors": {
                "saved_jobs": {
                    "error_type": "pagination_stopped",
                    "error_message": "LinkedIn did not keep offset 10",
                }
            },
        }
    )

    assert await saved_job_ids_from(jobs)() == ["1"]


async def test_a_refused_read_says_why():
    jobs = _jobs(
        {
            "url": "u",
            "sections": {},
            "section_errors": {
                "saved_jobs": {"error_type": "rate_limit", "error_message": "blocked"}
            },
        }
    )

    with pytest.raises(LinkedInOperationError, match="rate_limit: blocked"):
        await saved_job_ids_from(jobs)()
