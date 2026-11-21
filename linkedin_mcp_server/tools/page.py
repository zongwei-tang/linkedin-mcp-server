"""
Tools that read one LinkedIn page each: saved posts, groups, events, schools,
articles, newsletters, followers and the search verticals the other tools do
not cover.

Each is one navigation into one section, read as innerText with references,
and each reference argument is refused before the browser starts when it
names no page of its kind.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.pages import PageKind, page_target

logger = logging.getLogger(__name__)

MaxScrolls = Annotated[int, Field(ge=1, le=50)] | None
_READ = {"readOnlyHint": True, "openWorldHint": True}


async def _read(
    ctx: Context,
    tool_name: str,
    kind: PageKind,
    reference: str | None,
    max_scrolls: int | None,
) -> dict[str, Any]:
    # Refused before the browser, so a value that names no page costs no load.
    target = page_target(kind, reference)
    extractor = await get_ready_extractor(ctx, tool_name=tool_name)
    logger.info("%s: %s", tool_name, target.url)
    await ctx.report_progress(progress=0, total=100, message="Opening the page")
    result = await extractor.read_page(kind, reference, max_scrolls=max_scrolls)
    await ctx.report_progress(progress=100, total=100, message="Complete")
    return result


def register_page_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the single-page reading tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout, title="Get Saved Posts", annotations=_READ, tags={"post"}
    )
    @tool_errors
    async def get_saved_posts(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        List the posts and articles you saved on LinkedIn.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (saved_posts -> raw text) and optional
            references (the posts' authors and permalinks) and section_errors.
        """
        return await _read(ctx, "get_saved_posts", "saved_posts", None, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout, title="Get My Groups", annotations=_READ, tags={"network"}
    )
    @tool_errors
    async def get_my_groups(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        List the LinkedIn groups you belong to.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (groups -> raw text) and optional
            references (kind "group", /groups/<id>/) and section_errors.
            Read one with get_group_details.
        """
        return await _read(ctx, "get_my_groups", "my_groups", None, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout, title="Get Followers", annotations=_READ, tags={"network"}
    )
    @tool_errors
    async def get_followers(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        List the people who follow you on LinkedIn.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (followers -> raw text) and optional
            references (one profile per follower, at most 50) and
            section_errors.
        """
        return await _read(ctx, "get_followers", "followers", None, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout, title="Get Following", annotations=_READ, tags={"network"}
    )
    @tool_errors
    async def get_following(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        List the people and pages you follow on LinkedIn.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (following -> raw text) and optional
            references (profiles and pages, at most 50) and section_errors.
            Change one with unfollow_person or unfollow_company.
        """
        return await _read(ctx, "get_following", "following", None, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Group Details",
        annotations=_READ,
        tags={"network"},
    )
    @tool_errors
    async def get_group_details(
        group_id: str, ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read a LinkedIn group's page: its description and recent posts.

        Args:
            group_id: The group's numeric id, or a /groups/<id>/ link as
                get_my_groups and search_groups return it.
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more posts (1-50, default 10).

        Returns:
            Dict with url, sections (group -> raw text) and optional references
            and section_errors.
        """
        return await _read(ctx, "get_group_details", "group", group_id, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Event Details",
        annotations=_READ,
        tags={"network"},
    )
    @tool_errors
    async def get_event_details(
        event: str, ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read a LinkedIn event's page: when, where, organizer and details.

        Args:
            event: The event's id as its /events/<id>/ link carries it, or the
                link itself, as search_events returns it.
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations (1-50, default 5).

        Returns:
            Dict with url, sections (event -> raw text) and optional references
            and section_errors.
        """
        return await _read(ctx, "get_event_details", "event", event, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get School Profile",
        annotations=_READ,
        tags={"company"},
    )
    @tool_errors
    async def get_school_profile(
        school: str, ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read a school's or university's LinkedIn page.

        Args:
            school: The slug after /school/ (e.g. "stanford-university"), or a
                school page URL.
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations (1-50, default 5).

        Returns:
            Dict with url, sections (school -> raw text) and optional references
            and section_errors.
        """
        return await _read(ctx, "get_school_profile", "school", school, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get School Alumni",
        annotations=_READ,
        tags={"company"},
    )
    @tool_errors
    async def get_school_alumni(
        school: str, ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read a school's alumni page: where its alumni live and work, and who
        they are.

        Args:
            school: The slug after /school/ (e.g. "stanford-university"), or a
                school page URL.
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more alumni (1-50, default 5).

        Returns:
            Dict with url, sections (alumni -> raw text) and optional references
            (alumni profiles, at most 50) and section_errors.
        """
        return await _read(
            ctx, "get_school_alumni", "school_alumni", school, max_scrolls
        )

    @mcp.tool(
        timeout=tool_timeout, title="Get Article", annotations=_READ, tags={"post"}
    )
    @tool_errors
    async def get_article(
        article: str, ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read a LinkedIn article (a /pulse/ page) in full.

        Args:
            article: The slug after /pulse/, or the article's URL, as a kind
                "article" reference gives it.
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations (1-50, default 5).

        Returns:
            Dict with url, sections (article -> raw text) and optional
            references and section_errors.
        """
        return await _read(ctx, "get_article", "article", article, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout, title="Get Newsletter", annotations=_READ, tags={"post"}
    )
    @tool_errors
    async def get_newsletter(
        newsletter: str, ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read a LinkedIn newsletter's page: its description and editions.

        Args:
            newsletter: The segment after /newsletters/, or the newsletter's
                URL, as a kind "newsletter" reference gives it.
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations (1-50, default 5).

        Returns:
            Dict with url, sections (newsletter -> raw text) and optional
            references (its editions as kind "article") and section_errors.
        """
        return await _read(ctx, "get_newsletter", "newsletter", newsletter, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Groups",
        annotations=_READ,
        tags={"network", "search"},
    )
    @tool_errors
    async def search_groups(
        keywords: str, ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Find LinkedIn groups by keyword.

        Args:
            keywords: What to search for.
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations (1-50, default 5).

        Returns:
            Dict with url, sections (search_results -> raw text) and optional
            references (kind "group") and section_errors.
        """
        return await _read(ctx, "search_groups", "search_groups", keywords, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Events",
        annotations=_READ,
        tags={"network", "search"},
    )
    @tool_errors
    async def search_events(
        keywords: str, ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Find LinkedIn events by keyword.

        Args:
            keywords: What to search for.
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations (1-50, default 5).

        Returns:
            Dict with url, sections (search_results -> raw text) and optional
            references (kind "event") and section_errors.
        """
        return await _read(ctx, "search_events", "search_events", keywords, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Schools",
        annotations=_READ,
        tags={"company", "search"},
    )
    @tool_errors
    async def search_schools(
        keywords: str, ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Find schools and universities on LinkedIn by keyword.

        Args:
            keywords: What to search for.
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations (1-50, default 5).

        Returns:
            Dict with url, sections (search_results -> raw text) and optional
            references (kind "school") and section_errors. Read one with
            get_school_profile.
        """
        return await _read(
            ctx, "search_schools", "search_schools", keywords, max_scrolls
        )

    @mcp.tool(
        timeout=tool_timeout, title="Get My Events", annotations=_READ, tags={"network"}
    )
    @tool_errors
    async def get_my_events(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        List the LinkedIn events you are attending or organizing.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (events -> raw text) and optional
            references (the events and their organizers) and section_errors. A page LinkedIn answers with another
            page, even one in the same part of the site, comes back as a page_redirected section error.
        """
        return await _read(ctx, "get_my_events", "my_events", None, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Newsletters",
        annotations=_READ,
        tags={"network"},
    )
    @tool_errors
    async def get_my_newsletters(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        List the LinkedIn newsletters you subscribe to.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (newsletters -> raw text) and optional
            references (the newsletters and their authors) and section_errors. A page LinkedIn answers with another
            page, even one in the same part of the site, comes back as a page_redirected section error.
        """
        return await _read(
            ctx, "get_my_newsletters", "my_newsletters", None, max_scrolls
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Followed Pages",
        annotations=_READ,
        tags={"company"},
    )
    @tool_errors
    async def get_followed_pages(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        List the company, school and showcase pages you follow on LinkedIn.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (followed_pages -> raw text) and optional
            references (the pages: kind company, school or showcase; no tool
            reads a showcase page yet) and section_errors. A page LinkedIn answers with another
            page, even one in the same part of the site, comes back as a page_redirected section error.
        """
        return await _read(
            ctx, "get_followed_pages", "followed_pages", None, max_scrolls
        )

    @mcp.tool(
        timeout=tool_timeout, title="Get Catch Up", annotations=_READ, tags={"network"}
    )
    @tool_errors
    async def get_catch_up(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read your network's news: job changes, work anniversaries, birthdays
        and other updates from your connections.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (catch_up -> raw text) and optional
            references (the people each update is about) and section_errors. A page LinkedIn answers with another
            page, even one in the same part of the site, comes back as a page_redirected section error.
        """
        return await _read(ctx, "get_catch_up", "catch_up", None, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Suggested People",
        annotations=_READ,
        tags={"network"},
    )
    @tool_errors
    async def get_suggested_people(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        List the people LinkedIn suggests you may know, from your My Network page.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (people_you_may_know -> raw text) and optional
            references (the suggested profiles) and section_errors. A page LinkedIn answers with another
            page, even one in the same part of the site, comes back as a page_redirected section error.
        """
        return await _read(
            ctx, "get_suggested_people", "people_you_may_know", None, max_scrolls
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Dashboard",
        annotations=_READ,
        tags={"person"},
    )
    @tool_errors
    async def get_my_dashboard(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read your private dashboard: post impressions, followers, profile views
        and search appearances, as LinkedIn summarizes them.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (dashboard -> raw text) and optional
            references and section_errors. A page LinkedIn answers with another
            page, even one in the same part of the site, comes back as a page_redirected section error.
        """
        return await _read(ctx, "get_my_dashboard", "dashboard", None, max_scrolls)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Search Appearances",
        annotations=_READ,
        tags={"person"},
    )
    @tool_errors
    async def get_search_appearances(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read how often you appeared in LinkedIn search, and where the
        searchers work and what they do.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (search_appearances -> raw text) and optional
            references and section_errors. A page LinkedIn answers with another
            page, even one in the same part of the site, comes back as a page_redirected section error.
        """
        return await _read(
            ctx, "get_search_appearances", "search_appearances", None, max_scrolls
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Content Analytics",
        annotations=_READ,
        tags={"post"},
    )
    @tool_errors
    async def get_content_analytics(
        ctx: Context, max_scrolls: MaxScrolls = None
    ) -> dict[str, Any]:
        """
        Read the impressions and engagement of your posts over the recent
        period LinkedIn shows.

        Args:
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations to load more (1-50, default 10).

        Returns:
            Dict with url, sections (content_analytics -> raw text) and optional
            references (your top posts) and section_errors. A page LinkedIn answers with another
            page, even one in the same part of the site, comes back as a page_redirected section error.
        """
        return await _read(
            ctx, "get_content_analytics", "content_analytics", None, max_scrolls
        )
