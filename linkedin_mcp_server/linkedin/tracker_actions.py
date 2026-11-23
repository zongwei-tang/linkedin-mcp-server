"""Archive and unarchive one job in LinkedIn's job tracker.

Measured 2026-11-23 on `/jobs-tracker/?stage=applied` and `?stage=archived`,
English locale, read only, then verified by archiving a test job:

- The tracker renders every job twice, once per layout, and hides one of
  them. Each copy has its own overflow button,
  `button[componentkey^="opportunityTrackerOverflowMenuButton_"]` whose
  componentkey ends in `_<job id>` (`_card_<id>` hidden, `_table_<id>`
  shown at the server's window width), so the one to click is the visible one.
- Its menu (`[role="menu"]`) holds `role="menuitem"` items, each around an
  icon with an id: Archive is the one around `box-archive-outline-medium`. An
  archived job's menu offers Unarchive, around
  `box-archive-open-outline-medium`, and Delete (a closed posting's, only
  Delete).
- Unarchive asks for the stage to return to in a `[role="dialog"]` with one
  radio per stage, Saved then In Progress, drawn by its row so the input
  itself has no visible box (it is chosen with a DOM click), and two plain
  buttons, Cancel then Update.
- The stage lists page with a Next button around `chevron-right-outline-small`.

So the job is found by its id in an attribute, the item by its icon id, the
stage by its radio's place, and no label is read. An archive is confirmed by
finding the job in the archived stage, an unarchive by its leaving it.

Verified live 2026-11-23 on a test job: saved, archived (archived, then
already_archived, in a second process too), unarchived to Saved and found
there, unsaved.
"""

from __future__ import annotations

from typing import Any, Literal

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin.identifiers import job_view_url, normalize_job_id
from linkedin_mcp_server.linkedin.job_policy import SAVED_JOBS_URL
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

ArchiveStatus = Literal[
    "archived", "unarchived", "already_archived", "already_unarchived"
]
#: The stages a job is archived from, as `?stage=` spells them: the two
#: measured. (`?stage=in_progress` rendered no list on 2026-11-23.)
ArchivableStage = Literal["saved", "applied"]
#: The stages the unarchive dialog offers, in its order.
UnarchiveStage = Literal["saved", "in_progress"]
UNARCHIVE_STAGES: tuple[str, ...] = ("saved", "in_progress")

_JOB_MARK = "data-mcp-tracker-job"
_NEXT_MARK = "data-mcp-tracker-next"
_ITEM_MARK = "data-mcp-tracker-item"
_RADIO_MARK = "data-mcp-tracker-stage"
_UPDATE_MARK = "data-mcp-tracker-update"
_ARCHIVE_ICON = "box-archive-outline-medium"
_UNARCHIVE_ICON = "box-archive-open-outline-medium"
_READY_TIMEOUT = 12.0
_MENU_TIMEOUT = 5.0
_POLL = 0.25
_SETTLE = 1.5
_CLICK_ATTEMPTS = 3
_CLICK_TIMEOUT = 3000
DEFAULT_PAGES = 5

# The stage page once its list has rendered: whether the job is on it (its
# visible overflow button marked), and whether a Next button is.
TRACKER_JOB_JS = r"""([id, mark, next]) => {
    for (const name of [mark, next]) {
        for (const old of document.querySelectorAll('[' + name + ']')) old.removeAttribute(name);
    }
    const main = document.querySelector('main');
    if (!main) return {rendered: false, found: false, next: false};
    const shown = (el) => (el.checkVisibility ? el.checkVisibility() : el.getClientRects().length > 0);
    const menus = [...main.querySelectorAll(
        'button[componentkey^="opportunityTrackerOverflowMenuButton_"]'
    )].filter(shown);
    const mine = menus.filter((b) => b.getAttribute('componentkey').endsWith('_' + id));
    if (mine.length === 1) mine[0].setAttribute(mark, '');
    const nexts = [...main.querySelectorAll('button')].filter((b) => shown(b)
        && !b.disabled && b.getAttribute('aria-disabled') !== 'true'
        && b.querySelector('svg[id="chevron-right-outline-small"]'));
    if (nexts.length) nexts[nexts.length - 1].setAttribute(next, '');
    // Rendered: rows, or the empty list's link onward (no rows at all).
    const rendered = menus.length > 0 || !!main.querySelector('a[href*="/jobs/"]');
    return {rendered, found: mine.length === 1, next: nexts.length > 0,
            first: menus.length ? menus[0].getAttribute('componentkey') : null};
}"""

# The open menu's item around the icon *icon* (archive, or unarchive).
ARCHIVE_ITEM_JS = r"""([mark, icon]) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) old.removeAttribute(mark);
    const items = [...document.querySelectorAll('[role="menu"] [role="menuitem"]')]
        .filter((el) => el.getClientRects().length > 0);
    const archive = items.filter((el) => el.querySelector('svg[id="' + icon + '"]'));
    if (archive.length === 1) archive[0].setAttribute(mark, '');
    return {items: items.length, archive: archive.length};
}"""

# The unarchive dialog: the radio at *index* and the last plain button
# (Update) marked, once it shows its radios.
UNARCHIVE_DIALOG_JS = r"""([index, radio, update]) => {
    for (const name of [radio, update]) {
        for (const old of document.querySelectorAll('[' + name + ']')) old.removeAttribute(name);
    }
    const dialogs = [...document.querySelectorAll('[role="dialog"]')]
        .filter((d) => d.getClientRects().length > 0 && d.querySelector('input[type="radio"]'));
    if (dialogs.length !== 1) return {dialogs: dialogs.length, radios: 0};
    const radios = [...dialogs[0].querySelectorAll('input[type="radio"]')];
    const plain = [...dialogs[0].querySelectorAll('button')].filter((b) => !b.querySelector('svg'));
    if (radios.length <= index || plain.length < 2) {
        return {dialogs: 1, radios: radios.length, buttons: plain.length};
    }
    radios[index].setAttribute(radio, '');
    plain[plain.length - 1].setAttribute(update, '');
    return {dialogs: 1, radios: radios.length, buttons: plain.length, ready: true};
}"""


def stage_url(stage: str) -> str:
    return f"{SAVED_JOBS_URL}?stage={stage}"


class TrackerActions:
    """Archive jobs in the signed-in member's job tracker."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def archive_job(
        self,
        job_id: str,
        stage: ArchivableStage = "applied",
        max_pages: int = DEFAULT_PAGES,
    ) -> dict[str, Any]:
        """Archive one job found in one tracker stage.

        The stage is searched page by page up to *max_pages*. A job not found
        there but found in the archived stage is reported already_archived and
        left alone.

        Returns:
            {url, job_id, stage, status} where status is "archived" or
            "already_archived".
        """
        job_id = normalize_job_id(job_id)
        url = job_view_url(job_id)
        if not await self._find(job_id, stage, max_pages):
            if await self._find(job_id, "archived", max_pages):
                return _result(url, job_id, stage, "already_archived")
            raise LinkedInOperationError(
                f"Job {job_id} is neither in the first {max_pages} pages of the "
                f"tracker's {stage} stage nor in its archived stage. Nothing was "
                "archived."
            )
        await self._choose(job_id, _ARCHIVE_ICON, "archive")
        await self._session.delay(NAV_DELAY)
        try:
            archived = await self._find(job_id, "archived", max_pages)
        except LinkedInOperationError as exc:
            if type(exc) is not LinkedInOperationError:
                raise
            raise ActionOutcomeError(
                f"Archive was chosen for job {job_id}, but the archived stage "
                f"could not be read afterwards ({exc}). Read get_saved_jobs "
                "with stage archived before retrying."
            ) from exc
        if not archived:
            raise ActionOutcomeError(
                f"Archive was chosen for job {job_id}, but the tracker's "
                "archived stage does not list it, so LinkedIn may not have kept "
                "it. Calling this again is safe: a job already archived is "
                "found there first."
            )
        return _result(url, job_id, stage, "archived")

    async def unarchive_job(
        self,
        job_id: str,
        stage: UnarchiveStage = "saved",
        max_pages: int = DEFAULT_PAGES,
    ) -> dict[str, Any]:
        """Move one archived job back to the tracker stage *stage*.

        A job not in the archived stage's first *max_pages* pages is reported
        already_unarchived and left alone.

        Returns:
            {url, job_id, stage, status} where status is "unarchived" or
            "already_unarchived".
        """
        job_id = normalize_job_id(job_id)
        url = job_view_url(job_id)
        index = UNARCHIVE_STAGES.index(stage)
        if not await self._find(job_id, "archived", max_pages):
            return _result(url, job_id, stage, "already_unarchived")
        page = self._session.page
        await self._choose(job_id, _UNARCHIVE_ICON, "unarchive")
        args = [index, _RADIO_MARK, _UPDATE_MARK]
        deadline = self._session.monotonic() + _MENU_TIMEOUT
        dialog = await page.evaluate(UNARCHIVE_DIALOG_JS, args)
        while not dialog.get("ready") and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            dialog = await page.evaluate(UNARCHIVE_DIALOG_JS, args)
        if not dialog.get("ready"):
            await page.keyboard.press("Escape")
            raise LinkedInOperationError(
                f"Unarchive of job {job_id} opened {dialog['dialogs']} stage "
                f"dialogs with {dialog['radios']} stages, where one with Saved "
                "and In Progress was expected. It was closed and nothing was "
                "chosen."
            )
        async with linkedin_element(
            page.locator(f"input[{_RADIO_MARK}]"), timeout=5000
        ) as element:
            # The radio is drawn by its row, not itself: no pointer reaches it.
            await element.evaluate("(el) => el.click()")
        await self._click(f"button[{_UPDATE_MARK}]")
        await self._session.delay(NAV_DELAY)
        try:
            still = await self._find(job_id, "archived", max_pages)
        except LinkedInOperationError as exc:
            if type(exc) is not LinkedInOperationError:
                raise
            raise ActionOutcomeError(
                f"Unarchive was chosen for job {job_id}, but the archived stage "
                f"could not be read afterwards ({exc}). Read get_saved_jobs "
                "with stage archived before retrying."
            ) from exc
        if still:
            raise ActionOutcomeError(
                f"Unarchive was chosen for job {job_id}, but the tracker's "
                "archived stage still lists it, so LinkedIn may not have kept "
                "it. Calling this again is safe: it looks there first."
            )
        return _result(url, job_id, stage, "unarchived")

    async def _choose(self, job_id: str, icon: str, what: str) -> None:
        """Open the marked job's tracker menu and choose its item around *icon*."""
        page = self._session.page
        await self._click_job(job_id)
        args = [_ITEM_MARK, icon]
        deadline = self._session.monotonic() + _MENU_TIMEOUT
        item = await page.evaluate(ARCHIVE_ITEM_JS, args)
        while item["archive"] != 1 and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            item = await page.evaluate(ARCHIVE_ITEM_JS, args)
        if item["archive"] != 1:
            await page.keyboard.press("Escape")
            raise LinkedInOperationError(
                f"The tracker menu of job {job_id} showed {item['items']} items "
                f"and no single {what} item. It was closed and nothing was "
                "chosen."
            )
        await self._click(f"[{_ITEM_MARK}]")

    async def _click_job(self, job_id: str) -> None:
        """Click the job's visible tracker menu button, marked afresh.

        Measured live 2026-11-23: opening the stage the page already showed
        re-rendered the list after it was read, and the button marked then was
        gone. So it is marked again right before the click, and a click whose
        button went away is tried once more.
        """
        page = self._session.page
        for attempt in range(_CLICK_ATTEMPTS):
            found = await page.evaluate(TRACKER_JOB_JS, [job_id, _JOB_MARK, _NEXT_MARK])
            if not found["found"]:
                raise LinkedInOperationError(
                    f"Job {job_id} left the tracker page before its menu could "
                    "be opened. Nothing was chosen."
                )
            try:
                await self._click(f"button[{_JOB_MARK}]", timeout=_CLICK_TIMEOUT)
                return
            except PlaywrightTimeoutError:
                if attempt == _CLICK_ATTEMPTS - 1:
                    raise
                await self._session.delay(_SETTLE)

    async def _find(self, job_id: str, stage: str, max_pages: int) -> bool:
        """Whether the job is in *stage*, its overflow button marked if so."""
        page = self._session.page
        await self._navigator._navigate_to_page(stage_url(stage))
        await self._session.check_rate_limit()
        args = [job_id, _JOB_MARK, _NEXT_MARK]
        previous = None
        for _ in range(max(1, max_pages)):
            # A page has rendered once it shows rows other than the last
            # page's. An empty stage is known only once the wait has passed.
            deadline = self._session.monotonic() + _READY_TIMEOUT
            found = await page.evaluate(TRACKER_JOB_JS, args)
            while (
                not found["first"] or (previous and found["first"] == previous)
            ) and self._session.monotonic() < deadline:
                await self._session.delay(_POLL)
                found = await page.evaluate(TRACKER_JOB_JS, args)
            if not found["rendered"] or (previous and found["first"] == previous):
                raise LinkedInOperationError(
                    f"The job tracker's {stage} stage did not render its list."
                )
            if not found["found"]:
                # Rows render in one go, but give a late one its chance.
                await self._session.delay(_SETTLE)
                found = await page.evaluate(TRACKER_JOB_JS, args)
            if found["found"]:
                return True
            if not found["next"]:
                return False
            previous = found["first"]
            await self._click(f"button[{_NEXT_MARK}]")
        return False

    async def _click(self, selector: str, *, timeout: float = 5000) -> None:
        async with linkedin_element(
            self._session.page.locator(selector), timeout=timeout
        ) as element:
            await element.click(timeout=timeout)


def _result(url: str, job_id: str, stage: str, status: ArchiveStatus) -> dict[str, Any]:
    return {"url": url, "job_id": job_id, "stage": stage, "status": status}
