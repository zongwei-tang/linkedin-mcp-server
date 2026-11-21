"""Cloudflare's security check in front of LinkedIn, told apart from a page.

Measured 2026-11-21 from a datacenter host whose browser ran in Chromium's
headless mode: LinkedIn's public routes (/pulse/, /uas/login) answered with
Cloudflare's "Performing security verification" page on the page's own URL,
then with its Turnstile checkbox, and never issued a clearance. Every check
here is a mark Cloudflare's own markup or protocol carries, never its words,
which are translated:

- the ``cf-mitigated: challenge`` response header, which Cloudflare documents
  as the way to recognise a challenge response;
- the challenge platform's script (``/cdn-cgi/challenge-platform/``) and the
  ``__cf_chl_`` tokens it puts in the address and its form.

The page's own window object is not read: Patchright evaluates in an isolated
world, where the page's globals are not visible, but the DOM is.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .exceptions import BotChallengeError

CHALLENGE_HEADER = "cf-mitigated"

# Read as a locator count, like the other page checks, rather than a script:
# a count needs no page script and so runs on any document.
_CHALLENGE_MARKUP = (
    'script[src*="/cdn-cgi/challenge-platform/"], form[action*="__cf_chl_"]'
)


def response_is_challenge(response: Any) -> bool:
    """Whether *response* is Cloudflare's challenge, by its documented header."""
    try:
        headers = response.headers
        if not isinstance(headers, dict):
            return False
        value = headers.get(CHALLENGE_HEADER)
        return isinstance(value, str) and value.strip().lower() == "challenge"
    except Exception:
        return False


def url_is_challenge(url: str) -> bool:
    """Whether *url* carries the token Cloudflare adds while it checks."""
    return "__cf_chl_" in urlsplit(url or "").query


async def page_is_challenge(page: Any) -> bool:
    """Whether *page* shows Cloudflare's check rather than LinkedIn's page."""
    url = getattr(page, "url", "")
    if isinstance(url, str) and url_is_challenge(url):
        return True
    try:
        found = await page.locator(_CHALLENGE_MARKUP).count()
        # Only a real count is an answer.
        return isinstance(found, int) and not isinstance(found, bool) and found > 0
    except Exception:
        # A page that cannot be read says nothing either way.
        return False


async def raise_if_bot_challenge(page: Any, response: Any = None) -> None:
    """Raise BotChallengeError when the response or the page is the check."""
    if (response is not None and response_is_challenge(response)) or (
        await page_is_challenge(page)
    ):
        raise BotChallengeError()
