"""Browser-DOM tests for reading one settings category.

``SETTINGS_JS`` never runs under the unit suite's mocked ``page.evaluate``.
These drive ``SettingsReader.read_settings`` against a synthetic category page
in headless chromium, with navigation stubbed. The page carries what was
measured on 2026-11-23 and nothing else: LinkedIn's category navigation
(links into ``/mypreferences/d/categories/``, twice, as the page renders it)
and one link per setting into ``/mypreferences/``, its name on the first line
and, where the page shows one, its value below. Labels are German on purpose.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin import account_settings
from linkedin_mcp_server.linkedin.account_settings import (
    SettingsCategory,
    SettingsReader,
    category_url,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

NAV = "".join(
    f"<a href='/mypreferences/d/categories/{c}/'>{c}</a>"
    for c in ("account", "sign-in-and-security", "privacy", "notifications")
)


def setting(path: str, name: str, value: str | None = None) -> str:
    inner = f"<div><h3>{name}</h3></div>" + (
        f"<div><span>{value}</span></div>" if value else ""
    )
    return f"<a href='https://www.linkedin.com{path}'>{inner}</a>"


ACCOUNT = (
    f"<nav>{NAV}</nav><main><nav>{NAV}</nav><section><h2>Allgemein</h2>"
    + setting(
        "/mypreferences/m/settings/autoplay-videos/",
        "Videos automatisch abspielen",
        "An",
    )
    + setting("/mypreferences/m/dark-mode/", "Dunkelmodus")
    # The same setting linked twice is one setting.
    + setting("/mypreferences/m/dark-mode/", "Dunkelmodus")
    + setting(
        "/mypreferences/d/notification-categories/searching-for-a-job/",
        "Jobsuche",
    )
    + "</section></main>"
)


@pytest.fixture
async def dom_page():
    async def answer(route):
        await route.fulfill(status=200, content_type="text/html", body="<p>ok</p>")

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            context = await browser.new_context()
            page = await context.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        await context.route("**/*", answer)
        try:
            yield page
        finally:
            await browser.close()


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(account_settings, "_READY_TIMEOUT", 0.5)


async def read(
    page,
    html: str,
    category: SettingsCategory = "account",
    *,
    landed: str | None = None,
):
    session = PageSession(page)
    reader = SettingsReader(session, PageNavigator(session))

    async def navigate(url: str) -> None:
        await page.goto(landed or url)
        await page.set_content(html)

    with (
        patch.object(reader._navigator, "_navigate_to_page", side_effect=navigate),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
    ):
        return await reader.read_settings(category)


async def test_each_setting_comes_with_its_value_and_path(dom_page):
    result = await read(dom_page, ACCOUNT)

    assert (
        result["url"] == "https://www.linkedin.com/mypreferences/d/categories/account/"
    )
    assert result["category"] == "account"
    assert result["settings"] == [
        {
            "name": "Videos automatisch abspielen",
            "value": "An",
            "path": "/mypreferences/m/settings/autoplay-videos/",
        },
        {"name": "Dunkelmodus", "value": None, "path": "/mypreferences/m/dark-mode/"},
        {
            "name": "Jobsuche",
            "value": None,
            "path": "/mypreferences/d/notification-categories/searching-for-a-job/",
        },
    ]
    assert "Allgemein" in result["sections"]["settings"]


async def test_the_category_names_map_to_linkedins_pages(dom_page):
    result = await read(dom_page, ACCOUNT, "visibility")

    assert result["url"].endswith("/mypreferences/d/categories/profile-visibility/")


async def test_a_page_with_no_settings_is_a_section_error(dom_page):
    result = await read(dom_page, f"<main><nav>{NAV}</nav><p>Leer</p></main>")

    assert result["sections"] == {}
    assert result["section_errors"]["settings"]["error_type"] == "no_settings"


async def test_a_redirect_elsewhere_is_not_read_as_settings(dom_page):
    with pytest.raises(LinkedInOperationError, match="landed on"):
        await read(dom_page, ACCOUNT, landed="https://www.linkedin.com/feed/")


def test_an_unknown_category_names_the_known_ones():
    with pytest.raises(LinkedInOperationError, match="data_privacy"):
        category_url("billing")
