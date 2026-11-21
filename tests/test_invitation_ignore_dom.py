"""Browser-DOM tests for ignoring a received invitation.

``INVITATION_ROW_JS`` never executes under the unit suite's mocked
``page.evaluate``. These run ``InvitationActions.ignore_invitation`` against a
synthetic received list in headless chromium, with navigation stubbed. The
markup carries what was measured on 2026-11-20: one row per invitation keyed
``componentkey="invitation_manager_row_urn:li:invitation:<id>"``, the sender's
``/in/<vanity>/`` link, and two buttons, Ignore then Accept. Labels are
German on purpose: none of them may matter.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin import invitation_actions
from linkedin_mcp_server.linkedin.invitation_actions import InvitationActions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

URL = "https://www.linkedin.com/mynetwork/invitation-manager/received/"

# A click records which button of which row was pressed. Ignore removes its
# row, unless the page is told to ignore clicks.
CLICK_SCRIPT = """
for (const button of document.querySelectorAll('button')) {
    button.addEventListener('click', () => {
        const row = button.closest('[componentkey^="invitation_manager_row_"]');
        const chosen = JSON.parse(document.body.dataset.chosen || '[]');
        chosen.push(row.dataset.who + ':' + button.dataset.role);
        document.body.dataset.chosen = JSON.stringify(chosen);
        if (document.body.hasAttribute('data-ignore')) return;
        row.remove();
    });
}
"""

FILLER = "<p>" + "Einladungen verwalten. " * 10 + "</p>"


def row(who: str, invitation: str, *, buttons: str | None = None) -> str:
    buttons = (
        buttons
        if buttons is not None
        else (
            f'<button aria-label="Einladung von {who} ignorieren" data-role="ignore">'
            "Ignorieren</button>"
            f'<button aria-label="Einladung von {who} annehmen" data-role="accept">'
            "Annehmen</button>"
        )
    )
    return (
        f'<div componentkey="invitation_manager_row_urn:li:invitation:{invitation}" '
        f'data-who="{who}">'
        f'<div componentkey="urn:li:invitation:{invitation}" role="listitem">'
        f'<a href="https://www.linkedin.com/in/{who}/">{who}</a>'
        f"<p>möchte sich vernetzen</p><div>{buttons}</div></div></div>"
    )


def page(*rows: str) -> str:
    return f"<main>{FILLER}{''.join(rows)}</main><script>{CLICK_SCRIPT}</script>"


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
            dom = await context.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        await context.route("**/*", answer)
        try:
            yield dom
        finally:
            await browser.close()


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(invitation_actions, "_LIST_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(invitation_actions, "_IGNORE_TIMEOUT", 1.0)
    monkeypatch.setattr(invitation_actions, "NAV_DELAY", 0.0)


async def act(dom, html: str, username: str, *, reload_html: str | None = None):
    await dom.goto(URL)
    await dom.set_content(html)
    session = PageSession(dom)
    actions = InvitationActions(session, PageNavigator(session))
    loads = 0

    async def navigate(url: str) -> None:
        nonlocal loads
        loads += 1
        if loads > 1 and reload_html is not None:
            await dom.set_content(reload_html)

    with (
        patch.object(actions._navigator, "_navigate_to_page", side_effect=navigate),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
    ):
        return await actions.ignore_invitation(username)


async def chosen(dom) -> list[str]:
    return await dom.evaluate("() => JSON.parse(document.body.dataset.chosen || '[]')")


async def test_the_senders_first_action_is_clicked(dom_page):
    html = page(row("ada", "111"), row("grace", "222"))

    result = await act(dom_page, html, "grace")

    assert result == {
        "url": URL,
        "username": "grace",
        "invitation_id": "222",
        "status": "ignored",
    }
    assert await chosen(dom_page) == ["grace:ignore"]


async def test_a_full_profile_url_and_other_case_name_the_same_sender(dom_page):
    html = page(row("Ada-L", "111"), row("grace", "222"))

    result = await act(dom_page, html, "https://www.linkedin.com/in/ada-l/")

    assert result["invitation_id"] == "111"
    assert await chosen(dom_page) == ["Ada-L:ignore"]


async def test_a_name_that_only_starts_like_another_is_not_matched(dom_page):
    html = page(row("adam", "111"))

    with pytest.raises(LinkedInOperationError, match="No pending invitation from ada"):
        await act(dom_page, html, "ada")
    assert await chosen(dom_page) == []


async def test_a_row_without_exactly_two_actions_is_not_clicked(dom_page):
    one = '<button data-role="accept">Annehmen</button>'
    html = page(row("ada", "111", buttons=one))

    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, "ada")
    assert await chosen(dom_page) == []


async def test_a_row_that_stays_is_an_error(dom_page):
    await dom_page.goto(URL)
    await dom_page.set_content(page(row("ada", "111")))
    await dom_page.evaluate("() => document.body.setAttribute('data-ignore', '')")
    session = PageSession(dom_page)
    actions = InvitationActions(session, PageNavigator(session))
    with (
        patch.object(actions._navigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
        pytest.raises(ActionOutcomeError, match="stayed in the list"),
    ):
        await actions.ignore_invitation("ada")
    assert await chosen(dom_page) == ["ada:ignore"]


async def test_an_invitation_back_after_the_reload_is_an_error(dom_page):
    html = page(row("ada", "111"))

    with pytest.raises(ActionOutcomeError, match="back after a reload"):
        await act(dom_page, html, "ada", reload_html=page(row("ada", "111")))


async def test_the_last_invitation_leaving_an_empty_list_is_confirmed(dom_page):
    result = await act(dom_page, page(row("ada", "111")), "ada", reload_html=page())

    assert result["status"] == "ignored"


async def test_a_reload_that_renders_nothing_cannot_confirm(dom_page):
    with pytest.raises(ActionOutcomeError, match="did not render"):
        await act(dom_page, page(row("ada", "111")), "ada", reload_html="<main></main>")
