"""Browser-DOM tests for closing a blocking modal.

``handle_modal_close`` runs before every read, read-only tools included, so a
close button it clicks outside a dialog is a write: a page card's own X (a
suggestion's "Dismiss") would be dismissed for good. These drive it against a
synthetic page in headless chromium. The containers are synthetic on purpose:
they are a claim about the algorithm, not about LinkedIn's markup.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.utils import handle_modal_close

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

URL = "https://www.linkedin.com/mynetwork/grow/"


def button(who: str, label: str = "Dismiss") -> str:
    # Recorded on the body, because patchright evaluates in an isolated world.
    return (
        f'<button aria-label="{label}" '
        f"onclick=\"document.body.dataset.clicked = '{who}'\">×</button>"
    )


CARD = f"<main><section><div><p>Ada</p>{button('card')}</div></section></main>"


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


async def closed(page, html: str) -> tuple[bool, str | None]:
    await page.goto(URL)
    await page.set_content(html)
    result = await handle_modal_close(page)
    return result, await page.evaluate("() => document.body.dataset.clicked ?? null")


async def test_a_cards_dismiss_button_is_never_clicked(dom_page):
    assert await closed(dom_page, CARD) == (False, None)


async def test_the_open_dialogs_close_button_is_clicked_not_the_card_before_it(
    dom_page,
):
    html = CARD + f'<div role="dialog"><h2>Hinweis</h2>{button("dialog")}</div>'

    assert await closed(dom_page, html) == (True, "dialog")


async def test_a_closed_dialog_before_the_open_one_does_not_hide_it(dom_page):
    html = (
        CARD
        + f"<dialog>{button('closed', 'Close')}</dialog>"
        + f"<dialog open>{button('open', 'Close')}</dialog>"
    )

    assert await closed(dom_page, html) == (True, "open")
