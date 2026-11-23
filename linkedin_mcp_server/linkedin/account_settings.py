"""Read the signed-in member's settings, one category page at a time.

Measured 2026-11-23 on `/mypreferences/d/categories/account/`, English
locale, read only: each settings category is one page under
`/mypreferences/d/categories/<category>/`, and each setting on it is a link
into `/mypreferences/m/...`, whose text is the setting's name and, below it,
its current value where the page shows one ("Autoplay videos" / "On"). Other
categories link their settings elsewhere under `/mypreferences/`, so every
link there but the category navigation (`/mypreferences/d/categories/`)
counts.

Only the category pages LinkedIn's own settings navigation links are opened,
from a fixed list, and nothing on them is clicked. A setting's own page, where
it would be changed, is never opened.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

SettingsCategory = Literal[
    "account",
    "sign_in_and_security",
    "visibility",
    "data_privacy",
    "advertising_data",
    "notifications",
]

#: Each category's page, as LinkedIn's settings navigation links it.
CATEGORY_PATHS: dict[str, str] = {
    "account": "account",
    "sign_in_and_security": "sign-in-and-security",
    "visibility": "profile-visibility",
    "data_privacy": "privacy",
    "advertising_data": "ads",
    "notifications": "notifications",
}
SECTION = "settings"
_READY_TIMEOUT = 12.0
_POLL = 0.25

# The page's settings: every link in <main> into /mypreferences/ other than
# the category navigation itself, in page order.
SETTINGS_JS = r"""() => {
    const main = document.querySelector('main');
    if (!main) return null;
    const seen = new Set();
    const rows = [];
    for (const link of main.querySelectorAll('a[href*="/mypreferences/"]')) {
        let path = '';
        try { path = new URL(link.href).pathname; } catch (error) { continue; }
        if (seen.has(path) || path.startsWith('/mypreferences/d/categories/')) continue;
        seen.add(path);
        const lines = (link.innerText || '').split('\n').map((l) => l.trim()).filter(Boolean);
        if (!lines.length) continue;
        rows.push({name: lines[0], value: lines.slice(1).join(' ') || null, path});
    }
    return {rows, text: main.innerText || ''};
}"""


def category_url(category: str) -> str:
    """The page of one settings category."""
    try:
        path = CATEGORY_PATHS[category]
    except KeyError:
        raise LinkedInOperationError(
            f"Unknown settings category {category!r}; one of "
            + ", ".join(sorted(CATEGORY_PATHS))
            + "."
        ) from None
    return f"https://www.linkedin.com/mypreferences/d/categories/{path}/"


class SettingsReader:
    """Read the settings LinkedIn shows on one category page."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def read_settings(self, category: SettingsCategory) -> dict[str, Any]:
        """Read one settings category.

        Returns:
            {url, category, sections: {settings: text}, settings: [{name,
            value, path}]}, value null where the page shows none.
        """
        url = category_url(category)
        page = self._session.page
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        deadline = self._session.monotonic() + _READY_TIMEOUT
        found = await page.evaluate(SETTINGS_JS)
        while not (found and found["rows"]) and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await page.evaluate(SETTINGS_JS)
        landed = urlparse(page.url).path.rstrip("/")
        if landed != urlparse(url).path.rstrip("/"):
            raise LinkedInOperationError(
                f"Opening {url} landed on {page.url} instead, so its settings "
                "were not read."
            )
        if not (found and found["rows"]):
            return {
                "url": url,
                "category": category,
                "sections": {},
                "section_errors": {
                    SECTION: {
                        "error_type": "no_settings",
                        "error_message": f"{url} showed no settings links.",
                    }
                },
            }
        return {
            "url": url,
            "category": category,
            "sections": {SECTION: found["text"]},
            SECTION: found["rows"],
        }
