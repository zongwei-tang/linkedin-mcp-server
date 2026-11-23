"""Public page-reading facade and collaborator composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from patchright.async_api import Page

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.linkedin.capture import SectionCapture
from linkedin_mcp_server.linkedin.company import CompanyReader
from linkedin_mcp_server.linkedin.company_actions import CompanyActions
from linkedin_mcp_server.linkedin.connection_actions import ConnectionActions
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.contracts import (
    ExtractedSection as ExtractedSection,
    FilterValidationError as FilterValidationError,
    rate_limited_section_error as rate_limited_section_error,
)
from linkedin_mcp_server.linkedin.conversations import ConversationReader
from linkedin_mcp_server.linkedin.feed import FeedReader
from linkedin_mcp_server.linkedin.invitation_actions import InvitationActions
from linkedin_mcp_server.linkedin.account_settings import (
    SettingsCategory,
    SettingsReader,
)
from linkedin_mcp_server.linkedin.job_actions import JobActions, saved_job_ids_from
from linkedin_mcp_server.linkedin.job_alerts import JobAlertActions
from linkedin_mcp_server.linkedin.job_pages import JobPageReader
from linkedin_mcp_server.linkedin.job_policy import JobsTrackerStage
from linkedin_mcp_server.linkedin.jobs import JobReader
from linkedin_mcp_server.linkedin.message_sender import MessageSender
from linkedin_mcp_server.linkedin.mute_actions import MuteActions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.network import InvitationDirection, NetworkReader
from linkedin_mcp_server.linkedin.pages import PageKind, PageReader
from linkedin_mcp_server.linkedin.person import PersonReader
from linkedin_mcp_server.linkedin.person_actions import PersonActions
from linkedin_mcp_server.linkedin.post_actions import PostActions, Reaction
from linkedin_mcp_server.linkedin.post_reactions import PostReactionsReader
from linkedin_mcp_server.linkedin.post_save_actions import PostSaveActions
from linkedin_mcp_server.linkedin.conversation_flag_actions import (
    ConversationFlagActions,
    ThreadFlag,
)
from linkedin_mcp_server.linkedin.posts import PostSearch
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.tracker_actions import (
    ArchivableStage,
    TrackerActions,
    UnarchiveStage,
)
from linkedin_mcp_server.linkedin.text import (
    strip_conversation_chrome as strip_conversation_chrome,
    strip_linkedin_noise as strip_linkedin_noise,
)


if TYPE_CHECKING:
    from linkedin_mcp_server.callbacks import ProgressCallback


class LinkedInExtractor:
    """Compose page workflow owners and expose the stable tool-facing API."""

    def __init__(self, page: Page):
        session = PageSession(page)
        navigator = PageNavigator(session)
        content = PageContentReader(session)
        capture = SectionCapture(session, navigator, content)
        message_sender = MessageSender(session, navigator)
        profile_page = ProfilePageReader(
            session,
            lambda: message_sender._read_profile_message_target(),
        )
        person = PersonReader(session, navigator, capture, profile_page)

        self._content = content
        self._capture = capture
        self._feed = FeedReader(session, navigator, content)
        self._message_sender = message_sender
        self._person = person
        self._company = CompanyReader(session, capture)
        self._connection = ConnectionActions(
            session,
            navigator,
            lambda username: self.read_person(username, {"main_profile"}),
        )
        job_pages = JobPageReader(session, navigator, content)
        self._jobs = JobReader(navigator, capture, job_pages)
        self._posts = PostSearch(capture)
        self._network = NetworkReader(capture)
        self._pages = PageReader(capture, session)
        self._post_actions = PostActions(session, navigator)
        self._post_save_actions = PostSaveActions(session, navigator)
        self._post_reactions = PostReactionsReader(session, navigator)
        self._conversation_flag_actions = ConversationFlagActions(session, navigator)
        self._invitation_actions = InvitationActions(session, navigator)
        self._person_actions = PersonActions(session, navigator)
        self._company_actions = CompanyActions(session, navigator)
        self._job_actions = JobActions(
            session, navigator, saved_job_ids_from(self._jobs)
        )
        self._job_alerts = JobAlertActions(session, navigator)
        self._tracker_actions = TrackerActions(session, navigator)
        self._mute_actions = MuteActions(session, navigator)
        self._settings = SettingsReader(session, navigator)
        self._conversations = ConversationReader(
            session, navigator, content, profile_page
        )

    async def get_page_text(self) -> str:
        """Extract innerText from the main content area of the current page."""
        return await self._content.get_page_text()

    async def click_button_by_text(
        self, text: str, *, scope: str = "main", timeout: int = 5000
    ) -> bool:
        """Click the first button or link whose visible text exactly matches."""
        return await self._content.click_button_by_text(
            text, scope=scope, timeout=timeout
        )

    async def extract_feed(self, num_posts: int = 10) -> ExtractedSection:
        """Read the LinkedIn home feed, scrolling until enough posts load."""
        return await self._feed.extract_feed(num_posts)

    async def extract_page(
        self,
        url: str,
        section_name: str,
        max_scrolls: int | None = None,
    ) -> ExtractedSection:
        """Navigate, scroll to load lazy content, and extract innerText."""
        return await self._capture.extract_page(url, section_name, max_scrolls)

    async def read_person(
        self,
        username: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
        *,
        main_profile_already_loaded: bool = False,
        allow_self_alias: bool = False,
    ) -> dict[str, Any]:
        """Read a person profile with configurable sections."""
        return await self._person.read_person(
            username,
            requested,
            callbacks,
            max_scrolls,
            main_profile_already_loaded=main_profile_already_loaded,
            allow_self_alias=allow_self_alias,
        )

    async def get_my_profile(
        self,
        sections: set[str] | None = None,
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """Read the authenticated user's own LinkedIn profile."""
        return await self._person.get_my_profile(sections, callbacks, max_scrolls)

    async def connect_with_person(
        self,
        username: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Send a LinkedIn connection request or accept an incoming one."""
        return await self._connection.connect_with_person(username, note=note)

    async def get_sidebar_profiles(self, username: str) -> dict[str, Any]:
        """Extract profile links from sidebar sections on a profile page."""
        return await self._person.get_sidebar_profiles(username)

    async def read_company(
        self,
        company_name: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Read a company profile with configurable sections."""
        return await self._company.read_company(company_name, requested, callbacks)

    async def get_company_employees(
        self,
        company_name: str,
        keywords: str | None = None,
    ) -> dict[str, Any]:
        """List employees at a company from the people page."""
        return await self._company.get_company_employees(company_name, keywords)

    async def read_job(self, job_id: str) -> dict[str, Any]:
        """Read a single job posting."""
        return await self._jobs.read_job(job_id)

    async def get_job_apply_url(self, job_id: str) -> dict[str, Any]:
        """Read how a job posting takes applications and the employer's link."""
        return await self._jobs.get_job_apply_url(job_id)

    async def get_easy_apply_form(self, job_id: str) -> dict[str, Any]:
        """Read the questions of a job posting's Easy Apply form."""
        return await self._jobs.get_easy_apply_form(job_id)

    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Search for jobs with pagination and job ID extraction."""
        return await self._jobs.search_jobs(
            keywords,
            location,
            max_pages,
            date_posted,
            job_type,
            experience_level,
            work_type,
            easy_apply,
            sort_by,
            tool_timeout,
        )

    async def get_saved_jobs(
        self, max_pages: int = 3, stage: JobsTrackerStage = "saved"
    ) -> dict[str, Any]:
        """List the authenticated user's jobs at one job-tracker stage."""
        return await self._jobs.get_saved_jobs(max_pages, stage)

    async def dismiss_job(self, job_id: str) -> dict[str, Any]:
        """Dismiss a job from job search through its result card."""
        return await self._jobs.dismiss_job(job_id)

    async def search_people(
        self,
        keywords: str,
        location: str | None = None,
        network: list[str] | None = None,
        current_company: str | None = None,
        connection_of: str | None = None,
    ) -> dict[str, Any]:
        """Search for people and extract the results page."""
        return await self._person.search_people(
            keywords,
            location=location,
            network=network,
            current_company=current_company,
            connection_of=connection_of,
        )

    async def search_companies(self, keywords: str) -> dict[str, Any]:
        """Search for companies and extract the results page."""
        return await self._company.search_companies(keywords)

    async def search_posts(
        self,
        keywords: str,
        date_posted: str | None = None,
        max_pages: int = 3,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Search LinkedIn posts and extract the results page."""
        return await self._posts.search_posts(
            keywords,
            date_posted=date_posted,
            max_pages=max_pages,
            content_type=content_type,
        )

    async def read_post(
        self, post: str, max_scrolls: int | None = None
    ) -> dict[str, Any]:
        """Read one post and the comments its page renders."""
        return await self._posts.read_post(post, max_scrolls)

    async def set_post_liked(self, post: str, liked: bool) -> dict[str, Any]:
        """Like or unlike one post, leaving it as asked."""
        return await self._post_actions.set_post_liked(post, liked)

    async def read_post_reactions(
        self, post: str, max_scrolls: int | None = None
    ) -> dict[str, Any]:
        """Read who reacted to one post, and with which reaction."""
        return await self._post_reactions.read_post_reactions(post, max_scrolls)

    async def set_post_saved(self, post: str, saved: bool) -> dict[str, Any]:
        """Save or unsave one post, leaving it as asked."""
        return await self._post_save_actions.set_post_saved(post, saved)

    async def read_page(
        self,
        kind: PageKind,
        reference: str | None = None,
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """Read one page of a known kind into one section."""
        return await self._pages.read_page(kind, reference, max_scrolls)

    async def get_invitations(
        self,
        direction: InvitationDirection = "received",
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """List pending connection invitations, received or sent."""
        return await self._network.get_invitations(direction, max_scrolls)

    async def set_post_reaction(self, post: str, reaction: Reaction) -> dict[str, Any]:
        """Leave one post with exactly this reaction."""
        return await self._post_actions.set_post_reaction(post, reaction)

    async def set_person_followed(
        self, username: str, followed: bool
    ) -> dict[str, Any]:
        """Follow or unfollow one person, leaving them as asked."""
        return await self._person_actions.set_person_followed(username, followed)

    async def set_company_followed(
        self, company: str, followed: bool
    ) -> dict[str, Any]:
        """Follow or unfollow one company page, leaving it as asked."""
        return await self._company_actions.set_company_followed(company, followed)

    async def set_job_saved(self, job_id: str, saved: bool) -> dict[str, Any]:
        """Save or unsave one job posting, leaving it as asked."""
        return await self._job_actions.set_job_saved(job_id, saved)

    async def read_job_alerts(self) -> dict[str, Any]:
        """List the signed-in member's job alerts."""
        return await self._job_alerts.read_job_alerts()

    async def set_job_alert(
        self, keywords: str, location: str | None, on: bool
    ) -> dict[str, Any]:
        """Create or delete the job alert for one search, leaving it as asked."""
        return await self._job_alerts.set_job_alert(keywords, location, on)

    async def archive_job(
        self, job_id: str, stage: ArchivableStage = "applied"
    ) -> dict[str, Any]:
        """Archive one job in the job tracker."""
        return await self._tracker_actions.archive_job(job_id, stage)

    async def unarchive_job(
        self, job_id: str, stage: UnarchiveStage = "saved"
    ) -> dict[str, Any]:
        """Move one archived job back to a job tracker stage."""
        return await self._tracker_actions.unarchive_job(job_id, stage)

    async def set_person_muted(self, username: str, muted: bool) -> dict[str, Any]:
        """Mute or unmute one person, leaving them as asked."""
        return await self._mute_actions.set_person_muted(username, muted)

    async def read_settings(self, category: SettingsCategory) -> dict[str, Any]:
        """Read one category of the signed-in member's settings."""
        return await self._settings.read_settings(category)

    async def ignore_invitation(self, username: str) -> dict[str, Any]:
        """Ignore the pending invitation one person sent."""
        return await self._invitation_actions.ignore_invitation(username)

    async def get_connections(self, max_scrolls: int | None = None) -> dict[str, Any]:
        """List the signed-in member's connections."""
        return await self._network.get_connections(max_scrolls)

    async def get_notifications(self, max_scrolls: int | None = None) -> dict[str, Any]:
        """Read the signed-in member's notifications."""
        return await self._network.get_notifications(max_scrolls)

    async def get_profile_views(self, max_scrolls: int | None = None) -> dict[str, Any]:
        """Read who viewed the signed-in member's profile."""
        return await self._network.get_profile_views(max_scrolls)

    async def get_inbox(self, limit: int = 20) -> dict[str, Any]:
        """List recent conversations from the messaging inbox."""
        return await self._conversations.get_inbox(limit)

    async def get_conversation(
        self,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: int = 0,
    ) -> dict[str, Any]:
        """Read a specific messaging conversation by thread ID or username."""
        return await self._conversations.get_conversation(
            linkedin_username, thread_id, index
        )

    async def search_conversations(
        self, keywords: str, limit: int = 20
    ) -> dict[str, Any]:
        """Search messages by keyword."""
        return await self._conversations.search_conversations(keywords, limit)

    async def archive_conversation(self, thread_id: str) -> dict[str, Any]:
        """Archive one messaging thread by its thread id."""
        return await self._conversations.archive_conversation(thread_id)

    async def set_thread_flag(
        self, thread_id: str, flag: ThreadFlag, value: bool
    ) -> dict[str, Any]:
        """Star, unstar, mute or unmute one messaging thread."""
        return await self._conversation_flag_actions.set_thread_flag(
            thread_id, flag, value
        )

    async def set_thread_read(self, thread_id: str, read: bool) -> dict[str, Any]:
        """Mark one messaging thread read or unread."""
        return await self._conversation_flag_actions.set_thread_read(thread_id, read)

    async def send_message(
        self,
        linkedin_username: str,
        message: str,
        *,
        confirm_send: bool,
        profile_urn: str | None = None,
    ) -> dict[str, Any]:
        """Compose and send a new message with explicit confirmation gating."""
        return await self._message_sender.send_message(
            linkedin_username,
            message,
            confirm_send=confirm_send,
            profile_urn=profile_urn,
        )

    async def reply_to_conversation(
        self,
        thread_id: str,
        message: str,
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Reply inside an existing thread with explicit confirmation gating."""
        return await self._message_sender.reply_to_conversation(
            thread_id,
            message,
            confirm_send=confirm_send,
        )
