"""Workflows that read the signed-in member's own network pages."""

from __future__ import annotations

from typing import Any, Literal

from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.connection_actions import SENT_INVITATIONS_URL
from linkedin_mcp_server.linkedin.contracts import single_section_result

InvitationDirection = Literal["received", "sent"]

# One page per list. Each is the signed-in member's own, so none takes an
# identifier and none can be pointed at somebody else's network.
_LINKEDIN = "https://www.linkedin.com"
# The sent list is the page connect_with_person already reads to find an
# outstanding invitation; the received list is its sibling tab.
INVITATION_URLS: dict[str, str] = {
    "received": SENT_INVITATIONS_URL.replace("/sent/", "/received/"),
    "sent": SENT_INVITATIONS_URL,
}
CONNECTIONS_URL = f"{_LINKEDIN}/mynetwork/invite-connect/connections/"
NOTIFICATIONS_URL = f"{_LINKEDIN}/notifications/"
PROFILE_VIEWS_URL = f"{_LINKEDIN}/me/profile-views/"


class NetworkReader:
    """Own the reads of the signed-in member's invitations, connections,
    notifications and profile viewers.

    Every read is one capture of one page, so the section reader is the only
    collaborator. The lists load more entries as they are scrolled, which is
    the activity capture's behavior: wait for content, then scroll.
    """

    def __init__(self, capture: SectionCapture):
        self._capture = capture

    async def _read(
        self, url: str, section_name: str, max_scrolls: int | None
    ) -> dict[str, Any]:
        extracted = await self._capture.capture(
            url,
            section_name=section_name,
            plan=CapturePlan(CaptureMode.ACTIVITY, max_scrolls),
        )
        return single_section_result(url, section_name, extracted)

    async def get_invitations(
        self,
        direction: InvitationDirection = "received",
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """List pending connection invitations, received or sent.

        Returns:
            {url, sections: {invitations: text}} plus optional ``references``
            (the inviting or invited profiles) and ``section_errors``.
        """
        if direction not in INVITATION_URLS:
            raise ValueError(
                f"direction must be one of {sorted(INVITATION_URLS)}, not {direction!r}"
            )
        return await self._read(INVITATION_URLS[direction], "invitations", max_scrolls)

    async def get_connections(self, max_scrolls: int | None = None) -> dict[str, Any]:
        """List the signed-in member's first-degree connections, newest first.

        Returns:
            {url, sections: {connections: text}} plus optional ``references``
            (one profile per connection) and ``section_errors``.
        """
        return await self._read(CONNECTIONS_URL, "connections", max_scrolls)

    async def get_notifications(self, max_scrolls: int | None = None) -> dict[str, Any]:
        """Read the signed-in member's notifications page.

        Returns:
            {url, sections: {notifications: text}} plus optional
            ``references`` and ``section_errors``.
        """
        return await self._read(NOTIFICATIONS_URL, "notifications", max_scrolls)

    async def get_profile_views(self, max_scrolls: int | None = None) -> dict[str, Any]:
        """Read who viewed the signed-in member's profile.

        How many viewers LinkedIn names depends on the account's plan; the
        rest are described without a profile, and the text says so.

        Returns:
            {url, sections: {profile_views: text}} plus optional
            ``references`` and ``section_errors``.
        """
        return await self._read(PROFILE_VIEWS_URL, "profile_views", max_scrolls)
