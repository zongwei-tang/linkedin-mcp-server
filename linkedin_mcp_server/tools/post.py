"""
LinkedIn post/content search tool.

Performs LinkedIn's global content search (the "Posts" results tab) using
innerText extraction, so informal "we're hiring" / "Buscamos ..." posts can
be found before a formal job listing is published. Mirrors search_people:
build a /search/results/content/ URL, scroll to load results, and return the
raw innerText for the LLM to parse, plus post-permalink references.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.linkedin.contracts import FilterValidationError
from linkedin_mcp_server.linkedin.identifiers import normalize_post_reference
from linkedin_mcp_server.linkedin.search_urls import build_content_search_url

logger = logging.getLogger(__name__)


def register_post_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register post/content-search tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "search"},
    )
    @tool_errors
    async def search_posts(
        keywords: str,
        ctx: Context,
        date_posted: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """
        Search LinkedIn posts/content globally by keyword (the "Posts" tab).

        Use this to catch informal hiring posts ("we're hiring", "Buscamos
        ...", "estamos contratando", "join our team") that often appear before
        a formal job listing exists. This is global content search, distinct
        from get_feed (your own home feed) and get_company_posts (one
        company's page).

        Args:
            keywords: Search keywords (e.g., "Buscamos Unity", "AI automation hiring")
            ctx: FastMCP context for progress reporting
            date_posted: Optional recency filter. One of "past-24h",
                "past-week", "past-month"; the "past_24_hours" / "past_week" /
                "past_month" spellings used by search_jobs are accepted too.
                Omit for any time.
            content_type: Optional "Content type" filter. One of
                "documents" (PDFs and slide decks people upload), "images",
                "videos", "live_videos", "job_posts". Omit for all posts.
            max_pages: Scroll depth as result "pages" of ~5 scrolls each
                (1-10, default 3). Content search is an infinite scroll, so
                this caps how far the page is scrolled rather than fetching
                discrete pages.

        Returns:
            Dict with url, sections (search_results -> raw text), and optional
            references (post authors, companies, linked jobs, and kind
            "feed_post" permalinks read from the page's payload responses —
            /feed/update/<urn>/ or /posts/<slug>, both valid permalinks) and
            section_errors. The DOM carries no per-post permalink anchors;
            captured permalinks are not aligned to result order. The LLM
            should parse the raw text to extract each post's author,
            headline/role, company, body, posted date, and reaction/comment
            counts.
        """
        try:
            # Refused before the browser, as in search_people.
            build_content_search_url(
                keywords, date_posted=date_posted, content_type=content_type
            )
        except FilterValidationError as e:
            raise CodedToolError(str(e), ErrorCode.INVALID_ARGUMENT) from e

        extractor = await get_ready_extractor(ctx, tool_name="search_posts")
        logger.info(
            "Searching posts: keywords='%s', date_posted='%s', content_type='%s', "
            "max_pages=%d",
            keywords,
            date_posted,
            content_type,
            max_pages,
        )

        await ctx.report_progress(progress=0, total=100, message="Starting post search")

        try:
            result = await extractor.search_posts(
                keywords,
                date_posted=date_posted,
                content_type=content_type,
                max_pages=max_pages,
            )
        except FilterValidationError as e:
            # Validation messages carry actionable detail; surface them as
            # ToolError so mask_error_details doesn't reduce them to a
            # generic "Error calling tool 'search_posts'".
            raise CodedToolError(str(e), ErrorCode.INVALID_ARGUMENT) from e

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Post Details",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post"},
    )
    @tool_errors
    async def get_post_details(
        post: str,
        ctx: Context,
        max_scrolls: Annotated[int, Field(ge=1, le=30)] | None = None,
    ) -> dict[str, Any]:
        """
        Read one LinkedIn post in full, with the comments its page shows.

        Use it on a post another tool pointed at: every kind "feed_post"
        reference from get_feed, search_posts, get_company_posts and the
        posts section of a profile can be passed back as is.

        Args:
            post: The post, as a URN ("urn:li:activity:<id>", ugcPost and share
                URNs too) or a permalink ("/feed/update/<urn>/",
                "/posts/<slug>", or either one as a full URL).
            ctx: FastMCP context for progress reporting
            max_scrolls: Scroll iterations used to load comments (1-30,
                default 10). Replies hidden behind a "load more" control are
                not opened.

        Returns:
            Dict with url, sections (post -> raw text) and optional references
            (the author, commenters and anyone mentioned) and section_errors.
            The LLM should parse the raw text for the body, the author's
            headline, the date, reaction and comment counts and each comment.
        """
        # Refused before the browser, so a value that names no post costs no
        # page load.
        normalize_post_reference(post)
        extractor = await get_ready_extractor(ctx, tool_name="get_post_details")
        logger.info("Reading post: %s", post)

        await ctx.report_progress(progress=0, total=100, message="Reading the post")
        result = await extractor.read_post(post, max_scrolls=max_scrolls)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    _WRITE = {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }

    @mcp.tool(
        timeout=tool_timeout,
        title="Like Post",
        annotations=_WRITE,
        tags={"post", "actions"},
    )
    @tool_errors
    async def like_post(post: str, ctx: Context) -> dict[str, Any]:
        """
        Like one LinkedIn post as the signed-in member.

        The post's state is read first: a post you already like is left
        alone and reported already_liked, so a repeated call clicks nothing.
        The like is confirmed by loading the post again. Comments are never
        touched. Undo with unlike_post.

        Args:
            post: The post, in any form get_post_details takes: a URN
                ("urn:li:activity:<id>") or a "feed_post" reference
                ("/feed/update/<urn>/" or "/posts/<slug>").
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, post (its path), liked (true) and status: "liked"
            or "already_liked".
        """
        normalize_post_reference(post)
        extractor = await get_ready_extractor(ctx, tool_name="like_post")
        logger.info("Liking post: %s", post)
        await ctx.report_progress(progress=0, total=100, message="Opening the post")
        result = await extractor.set_post_liked(post, True)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Unlike Post",
        annotations=_WRITE,
        tags={"post", "actions"},
    )
    @tool_errors
    async def unlike_post(post: str, ctx: Context) -> dict[str, Any]:
        """
        Remove the signed-in member's like, or any other reaction, from a post.

        A post you have not reacted to is left alone and reported
        already_unliked, so a repeated call clicks nothing. The change is
        confirmed by loading the post again. Comments are never touched.

        Args:
            post: The post, in any form get_post_details takes: a URN
                ("urn:li:activity:<id>") or a "feed_post" reference
                ("/feed/update/<urn>/" or "/posts/<slug>").
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, post (its path), liked (false) and status:
            "unliked" or "already_unliked".
        """
        normalize_post_reference(post)
        extractor = await get_ready_extractor(ctx, tool_name="unlike_post")
        logger.info("Unliking post: %s", post)
        await ctx.report_progress(progress=0, total=100, message="Opening the post")
        result = await extractor.set_post_liked(post, False)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="React to Post",
        annotations=_WRITE,
        tags={"post", "actions"},
    )
    @tool_errors
    async def react_to_post(
        post: str,
        reaction: Literal[
            "like", "celebrate", "support", "love", "insightful", "funny"
        ],
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Leave one LinkedIn post with exactly this reaction from you.

        Replaces a different reaction you already gave it. A post already
        showing this reaction is left alone and reported already_reacted, so a
        repeated call clicks nothing. The reaction is confirmed by loading the
        post again. Take any reaction back with unlike_post.

        Args:
            post: The post, in any form get_post_details takes: a URN
                ("urn:li:activity:<id>") or a "feed_post" reference
                ("/feed/update/<urn>/" or "/posts/<slug>").
            reaction: One of "like", "celebrate", "support", "love",
                "insightful", "funny".
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, post (its path), reaction and status: "reacted" or
            "already_reacted".
        """
        normalize_post_reference(post)
        extractor = await get_ready_extractor(ctx, tool_name="react_to_post")
        logger.info("Reacting %s to post: %s", reaction, post)
        await ctx.report_progress(progress=0, total=100, message="Opening the post")
        result = await extractor.set_post_reaction(post, reaction)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result
