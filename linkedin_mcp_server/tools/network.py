"""
Tools that read the signed-in member's own network pages.

Pending invitations, connections, notifications and profile viewers. Each is
one page of the member's own account, read as innerText with profile
references, so none of them takes an identifier.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors

logger = logging.getLogger(__name__)

MaxScrolls = Annotated[int, Field(ge=1, le=50)] | None


def register_network_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the network-reading tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"network"},
    )
    @tool_errors
    async def get_invitations(
        ctx: Context,
        direction: Literal["received", "sent"] = "received",
        max_scrolls: MaxScrolls = None,
    ) -> dict[str, Any]:
        """
        List your pending connection invitations.

        Args:
            ctx: FastMCP context for progress reporting
            direction: "received" (default) for invitations waiting on you,
                "sent" for the ones you sent that are still pending.
            max_scrolls: Scroll iterations to load more entries (1-50,
                default 10).

        Returns:
            Dict with url, sections (invitations -> raw text) and optional
            references (one profile per invitation) and section_errors.
            Accept a received invitation with connect_with_person on the
            profile it references.
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_invitations")
        logger.info("Reading %s invitations", direction)
        await ctx.report_progress(
            progress=0, total=100, message=f"Reading {direction} invitations"
        )
        result = await extractor.get_invitations(direction, max_scrolls=max_scrolls)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Connections",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"network"},
    )
    @tool_errors
    async def get_connections(
        ctx: Context,
        max_scrolls: MaxScrolls = None,
    ) -> dict[str, Any]:
        """
        List your first-degree connections, most recently added first.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more connections (1-50,
                default 10). Each one loads roughly one more batch.

        Returns:
            Dict with url, sections (connections -> raw text) and optional
            references (one profile per connection, at most 50) and
            section_errors.
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_connections")
        await ctx.report_progress(
            progress=0, total=100, message="Reading your connections"
        )
        result = await extractor.get_connections(max_scrolls=max_scrolls)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Notifications",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"network"},
    )
    @tool_errors
    async def get_notifications(
        ctx: Context,
        max_scrolls: MaxScrolls = None,
    ) -> dict[str, Any]:
        """
        Read your LinkedIn notifications, newest first.

        Opening the notifications page marks them seen, exactly as it does
        for you in a browser.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load older notifications (1-50,
                default 10).

        Returns:
            Dict with url, sections (notifications -> raw text) and optional
            references (the people, companies, posts and jobs they mention)
            and section_errors.
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_notifications")
        await ctx.report_progress(
            progress=0, total=100, message="Reading your notifications"
        )
        result = await extractor.get_notifications(max_scrolls=max_scrolls)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Profile Views",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"network"},
    )
    @tool_errors
    async def get_profile_views(
        ctx: Context,
        max_scrolls: MaxScrolls = None,
    ) -> dict[str, Any]:
        """
        Read who viewed your profile recently.

        How many viewers LinkedIn names depends on your plan; the others are
        described without a profile.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more viewers (1-50,
                default 10).

        Returns:
            Dict with url, sections (profile_views -> raw text) and optional
            references (the viewers LinkedIn names) and section_errors.
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_profile_views")
        await ctx.report_progress(
            progress=0, total=100, message="Reading your profile views"
        )
        result = await extractor.get_profile_views(max_scrolls=max_scrolls)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result
