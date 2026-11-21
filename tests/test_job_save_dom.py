"""Browser-DOM tests for saving and unsaving a job posting.

``SAVE_BUTTON_JS`` never runs under the unit suite's mocked ``page.evaluate``.
These drive ``JobActions.set_job_saved`` against a synthetic posting in
headless chromium, with navigation stubbed and the saved-jobs list played by
a fake that follows the page. The posting behaves as measured on 2026-11-20:
the apply link (the safety redirect, or the posting's `/apply` path) shares a
row with exactly one other button, Save, whose only change when clicked is its
words. Both carry an aria-label. Labels are German on purpose.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import (
    AccountRestrictedError,
    ActionOutcomeError,
    AuthenticationError,
    LinkedInOperationError,
    RateLimitError,
)
from linkedin_mcp_server.linkedin import job_actions
from linkedin_mcp_server.linkedin.job_actions import JobActions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

JOB = "4493253527"
URL = f"https://www.linkedin.com/jobs/view/{JOB}/"

# A click on Save flips its words and records itself, unless told to ignore it.
SCRIPT = """
const save = document.querySelector('[data-who="save"]');
save.addEventListener('click', () => {
    const clicks = Number(document.body.dataset.clicks || '0') + 1;
    document.body.dataset.clicks = String(clicks);
    if (document.body.hasAttribute('data-ignore')) return;
    const saved = save.dataset.saved !== 'true';
    save.dataset.saved = String(saved);
    save.textContent = saved ? 'Gespeichert' : 'Speichern';
    if (save.hasAttribute('aria-label')) {
        save.setAttribute('aria-label', saved ? 'Job nicht mehr speichern' : 'Job speichern');
    }
});
"""


def posting(
    *,
    saved: bool = False,
    apply: str = "/safety/go/?url=x",
    extra: str = "",
    labelled: bool = True,
    description: str = "",
) -> str:
    words = "Gespeichert" if saved else "Speichern"
    label = (
        f' aria-label="{"Job nicht mehr speichern" if saved else "Job speichern"}"'
        if labelled
        else ""
    )
    return (
        "<main><div><div>"
        '<div><a href="https://www.linkedin.com/company/acme/">Acme</a>'
        '<button aria-expanded="false" aria-label="Weitere Optionen"></button></div>'
        "<div><div><div>"
        f'<div><a href="https://www.linkedin.com{apply}" aria-label="Bewerben">Bewerben</a></div>'
        f'<div><button data-who="save" data-saved="{str(saved).lower()}"{label}>{words}</button></div>'
        f"{extra}"
        "</div></div></div>"
        "</div></div>"
        f"{description}"
        '<section><button aria-label="Acme folgen">Folgen</button></section>'
        f"</main><script>{SCRIPT}</script>"
    )


class SavedList:
    """The saved-jobs list as the tracker reads it: what the page says, or a
    posting saved long ago that the pages read never reach."""

    def __init__(
        self,
        page,
        *,
        deep: bool = False,
        fail_on_read: dict[int, Exception] | None = None,
    ):
        self.page = page
        self.deep = deep
        self.fail_on_read = fail_on_read or {}
        self.reads = 0

    async def __call__(self) -> list[str]:
        self.reads += 1
        if self.reads in self.fail_on_read:
            raise self.fail_on_read[self.reads]
        saved = await self.page.evaluate(
            "() => document.querySelector('[data-who=\"save\"]').dataset.saved === 'true'"
        )
        if not saved:
            return ["111"]
        # A deep-saved posting is not on the pages read until saved again.
        return ["111"] if self.deep and self.reads == 1 else [JOB, "111"]


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
    monkeypatch.setattr(job_actions, "_POSTING_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(job_actions, "_TOGGLE_TIMEOUT", 1.0)
    monkeypatch.setattr(job_actions, "NAV_DELAY", 0.0)


async def act(
    page,
    html: str,
    saved: bool,
    *,
    deep: bool = False,
    ignore: bool = False,
    gone_on_load: int | None = None,
    ignore_on_load: int | None = None,
    fail_on_load: dict[int, Exception] | None = None,
    fail_on_read: dict[int, Exception] | None = None,
    saved_ids: list[str] | None = None,
):
    """Run one save or unsave.

    Loads of the posting and reads of the saved list count from one:
    ``gone_on_load`` takes the Save button off the page at that load,
    ``ignore_on_load`` makes the page ignore clicks from that load on, and
    ``fail_on_load``/``fail_on_read`` raise at those. ``saved_ids`` is a saved
    list that never changes, whatever the button does.
    """
    await page.goto(URL)
    await page.set_content(html)
    if ignore:
        await page.evaluate("() => document.body.setAttribute('data-ignore', '')")
    session = PageSession(page)
    saved_list = (
        AsyncMock(return_value=saved_ids)
        if saved_ids is not None
        else SavedList(page, deep=deep, fail_on_read=fail_on_read)
    )
    actions = JobActions(session, PageNavigator(session), saved_list)
    loads = 0

    async def load(_url):
        nonlocal loads
        loads += 1
        if fail_on_load and loads in fail_on_load:
            raise fail_on_load[loads]
        if loads == gone_on_load:
            await page.evaluate(
                "() => document.querySelector('[data-who=\"save\"]').remove()"
            )
        if loads == ignore_on_load:
            await page.evaluate("() => document.body.setAttribute('data-ignore', '')")

    with (
        patch.object(actions._navigator, "_navigate_to_page", side_effect=load),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
    ):
        return await actions.set_job_saved(JOB, saved)


async def clicks(page) -> int:
    return int(await page.evaluate("() => document.body.dataset.clicks || '0'"))


async def test_saving_clicks_the_button_beside_the_apply_link(dom_page):
    result = await act(dom_page, posting(), True)

    assert result == {"url": URL, "job_id": JOB, "saved": True, "status": "saved"}
    assert await clicks(dom_page) == 1


async def test_an_easy_apply_posting_is_found_by_its_apply_path(dom_page):
    html = posting(apply=f"/jobs/view/{JOB}/apply/?openSDUIApplyFlow=true")

    result = await act(dom_page, html, True)

    assert result["status"] == "saved"


async def test_a_listed_posting_is_not_saved_again(dom_page):
    result = await act(dom_page, posting(saved=True), True)

    assert result["status"] == "already_saved"
    assert await clicks(dom_page) == 0


async def test_unsaving_a_listed_posting(dom_page):
    result = await act(dom_page, posting(saved=True), False)

    assert result["status"] == "unsaved"
    assert await clicks(dom_page) == 1


async def test_unsaving_an_unsaved_posting_puts_back_the_save_it_made(dom_page):
    # Not listed, so it might be saved past the pages read: one click is
    # tried, it saves, and a second click puts the posting back as found.
    result = await act(dom_page, posting(saved=False), False)

    assert result["status"] == "already_unsaved"
    assert await clicks(dom_page) == 2


async def test_saving_a_posting_saved_past_the_pages_read_restores_it(dom_page):
    result = await act(dom_page, posting(saved=True), True, deep=True)

    assert result["status"] == "already_saved"
    assert await clicks(dom_page) == 2


async def test_a_row_with_two_buttons_is_not_clicked(dom_page):
    html = posting(extra="<div><button>Teilen</button></div>")

    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, True)
    assert await clicks(dom_page) == 0


async def test_a_posting_without_an_apply_link_is_not_clicked(dom_page):
    html = posting(apply="/company/acme/")

    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, True)
    assert await clicks(dom_page) == 0


async def test_a_click_that_changes_nothing_is_an_error(dom_page):
    # Clicked, so the account may have changed: an unknown outcome.
    with pytest.raises(ActionOutcomeError, match="did not change"):
        await act(dom_page, posting(), True, ignore=True)


async def test_a_listed_posting_still_listed_after_its_click_is_an_error(dom_page):
    with pytest.raises(ActionOutcomeError, match="still is after its"):
        await act(dom_page, posting(saved=True), False, saved_ids=[JOB, "111"])
    assert await clicks(dom_page) == 1


async def test_a_list_that_never_takes_the_posting_after_two_clicks_is_an_error(
    dom_page,
):
    with pytest.raises(ActionOutcomeError, match="clicked twice, but"):
        await act(dom_page, posting(saved=False), True, saved_ids=["111"])
    assert await clicks(dom_page) == 2


@pytest.mark.parametrize(
    ("refusal", "unsave_listed", "fail_on_load", "fail_on_read", "made"),
    [
        # Reading the list after the one click: unsaving a listed posting.
        (AuthenticationError("signed out"), True, None, 2, 1),
        (AccountRestrictedError(), True, None, 2, 1),
        (RateLimitError("slow down", suggested_wait_time=30), True, None, 2, 1),
        # Opening the posting to click it back, after unsaving an unlisted one
        # saved it.
        (RateLimitError("slow down", suggested_wait_time=30), False, 2, None, 1),
        # Reading the list after the click back.
        (AuthenticationError("signed out"), False, None, 3, 2),
    ],
    ids=[
        "auth-after-click",
        "restricted-after-click",
        "rate-limit-after-click",
        "rate-limit-before-click-back",
        "auth-after-click-back",
    ],
)
async def test_a_refusal_after_a_click_keeps_its_own_error(
    dom_page, refusal, unsave_listed, fail_on_load, fail_on_read, made
):
    # Its code, retry_after and the relogin it starts would be lost as an
    # unknown outcome.
    with pytest.raises(type(refusal)) as caught:
        await act(
            dom_page,
            posting(saved=unsave_listed),
            False,
            fail_on_load={fail_on_load: refusal} if fail_on_load else None,
            fail_on_read={fail_on_read: refusal} if fail_on_read else None,
        )
    assert caught.value is refusal
    assert await clicks(dom_page) == made


async def test_a_list_unread_after_a_confirmed_click_back_does_not_say_flipped(
    dom_page,
):
    # The click back changed the button, so the posting is most likely as
    # asked: only the last read of the list failed.
    unread = LinkedInOperationError("The saved-jobs list could not be read")

    with pytest.raises(ActionOutcomeError) as caught:
        await act(dom_page, posting(saved=False), False, fail_on_read={3: unread})

    message = str(caught.value)
    assert "could not be read after it" in message
    assert "is now saved" not in message
    assert "the click to put it back failed" not in message
    assert await clicks(dom_page) == 2


async def test_an_unconfirmed_click_back_says_the_state_is_unknown(dom_page):
    with pytest.raises(ActionOutcomeError) as caught:
        await act(dom_page, posting(saved=False), False, ignore_on_load=2)

    message = str(caught.value)
    assert "Whether it is saved is unknown" in message
    assert "is now saved" not in message
    assert await clicks(dom_page) == 2


async def test_an_unlabelled_button_beside_the_apply_link_is_not_clicked(dom_page):
    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, posting(labelled=False), True)
    assert await clicks(dom_page) == 0


async def test_a_description_link_is_not_taken_for_the_apply_link(dom_page):
    # An applied or closed posting shows no apply link of its own, while its
    # description can still hold a link through the same safety redirect,
    # here beside the company's Follow button.
    description = (
        "<section><div><p>Mehr unter "
        '<a href="https://www.linkedin.com/safety/go/?url=y">acme.example</a></p>'
        '<button aria-label="Acme folgen" '
        "onclick=\"document.body.dataset.wrong = 'true'\">Folgen</button>"
        "</div></section>"
    )
    html = posting(apply="/company/acme/", description=description)

    with pytest.raises(LinkedInOperationError, match="Nothing was clicked"):
        await act(dom_page, html, True)
    assert await clicks(dom_page) == 0
    assert await dom_page.evaluate("() => document.body.dataset.wrong") is None


async def test_a_failed_click_back_says_the_posting_was_left_flipped(dom_page):
    # Unlisted, so unsaving clicks once to tell the state apart, which saves
    # it; the button is gone when the posting is opened to click it back.
    with pytest.raises(ActionOutcomeError) as caught:
        await act(dom_page, posting(saved=False), False, gone_on_load=2)

    message = str(caught.value)
    assert "is now saved, the opposite of what was asked" in message
    assert "Call unsave_job again" in message
    assert "Nothing was clicked" not in message
    assert await clicks(dom_page) == 1
