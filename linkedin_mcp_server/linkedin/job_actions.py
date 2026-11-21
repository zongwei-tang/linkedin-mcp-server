"""Write actions on a job posting: save and unsave.

Measured 2026-11-20 on an external-apply posting, English locale, by saving it
and unsaving it again:

- The Save control is the one button sharing the apply link's row: the link
  is the safety redirect (`/safety/go`) or the posting's own `/apply` path,
  and the closest ancestor above it that holds any other button holds exactly
  this one.
- Both the apply link and the Save button carry an aria-label; the
  description's "… more" button carries none. Requiring the attribute on both
  keeps a link written into the description, which LinkedIn sends through the
  same safety redirect and which is not expected to carry one, from being
  taken for the apply link on a posting that shows none of its own (an
  applied or closed one), and the button beside it from being clicked.
- Saved and unsaved differ only in the button's words ("Save"/"Saved" and
  their aria-labels). It carries no icon and no pressed state, and its
  componentkey is new on every load. The saved-jobs list is the one
  locale-independent record of the state: it holds the posting once saved and
  drops it once unsaved.

So the state is read from the saved-jobs list, never from the button. A
posting saved so long ago that it sits past the pages read looks unsaved;
when a click then takes the posting out of the list rather than into it, the
button is clicked once more to put it back, and the posting is reported as it
was found. The click itself is confirmed by the button's text changing,
without reading what either text says.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin.identifiers import job_view_url, normalize_job_id
from linkedin_mcp_server.linkedin.job_policy import SAFETY_REDIRECT_PATH
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

if TYPE_CHECKING:
    from linkedin_mcp_server.linkedin.jobs import JobReader

SaveStatus = Literal["saved", "unsaved", "already_saved", "already_unsaved"]
SavedJobIds = Callable[[], Awaitable[list[str]]]

#: How many pages of the saved list are read for the state. A posting saved
#: further back is handled by the second click, not missed.
SAVED_PAGES_READ = 3

#: The saved list's own way of saying it ends where LinkedIn stopped serving
#: it. The tracker drops the `start` offset and serves the first page again
#: (see JobReader.get_saved_jobs), so every list longer than one page ends this
#: way. That is a list read as far as it can be read, not a failed read: a
#: posting past it is the second-click case above.
_LIST_ENDS = frozenset({"pagination_stopped"})


def saved_job_ids_from(jobs: JobReader) -> SavedJobIds:
    """The saved-jobs list as job ids, refusing a read that failed.

    A failed read would look like an empty list, and an empty list would make
    every posting look unsaved.
    """

    async def saved_job_ids() -> list[str]:
        listed = await jobs.get_saved_jobs(max_pages=SAVED_PAGES_READ, stage="saved")
        errors = listed.get("section_errors") or {}
        failure = next(
            (
                error
                for error in errors.values()
                if error.get("error_type") not in _LIST_ENDS
            ),
            None,
        )
        if failure is not None:
            raise LinkedInOperationError(
                "The saved-jobs list could not be read, so whether the posting "
                f"is saved is unknown ({failure.get('error_type')}: "
                f"{failure.get('error_message')}). Check get_saved_jobs before "
                "retrying."
            )
        return [str(job) for job in listed.get("job_ids") or []]

    return saved_job_ids


_SAVE_MARK = "data-mcp-save"
_POSTING_READY_TIMEOUT = 10.0
_TOGGLE_TIMEOUT = 6.0
_POLL = 0.25

# The one button beside the apply link, marked, with its current text so a
# click can be confirmed by the text changing.
SAVE_BUTTON_JS = r"""(opts) => {
    const {applyPath, redirectPath, mark} = opts;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const main = document.querySelector('main');
    const result = {found: false, buttons: 0, labelled: false, text: null};
    if (!main) return result;
    const pathOf = (anchor) => {
        try {
            return new URL(anchor.href).pathname.replace(/\/+$/, '');
        } catch (error) {
            return '';
        }
    };
    // Presence only: the label's words are the page's language.
    const apply = [...main.querySelectorAll('a[href][aria-label]')].find(
        (anchor) => [applyPath, redirectPath].includes(pathOf(anchor))
    );
    if (!apply) return result;
    let node = apply.parentElement;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
        const buttons = [...node.querySelectorAll('button')].filter(
            (button) => !apply.contains(button)
        );
        if (!buttons.length) continue;
        result.buttons = buttons.length;
        result.labelled = buttons.length === 1 && buttons[0].hasAttribute('aria-label');
        if (result.labelled) {
            result.found = true;
            result.text = (buttons[0].innerText || '').trim();
            buttons[0].setAttribute(mark, '');
        }
        return result;
    }
    return result;
}"""


class JobActions:
    """Own the actions the signed-in member takes on a job posting."""

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        saved_job_ids: SavedJobIds,
    ):
        self._session = session
        self._navigator = navigator
        self._saved_job_ids = saved_job_ids

    async def set_job_saved(self, job_id: str, saved: bool) -> dict[str, Any]:
        """Save or unsave one posting, leaving it as asked.

        Returns:
            {url, job_id, saved, status} where status is "saved", "unsaved",
            "already_saved" or "already_unsaved".
        """
        job_id = normalize_job_id(job_id)
        url = job_view_url(job_id, "/")
        listed = job_id in await self._saved_job_ids()
        if listed and saved:
            return _result(url, job_id, saved, "already_saved")

        await self._toggle(url, job_id)
        try:
            now = job_id in await self._saved_job_ids()
        except ActionOutcomeError:
            raise
        except LinkedInOperationError as exc:
            raise ActionOutcomeError(
                f"Job {job_id}'s Save button was clicked, but the saved list "
                f"could not be read after it ({exc}). Check get_saved_jobs "
                "before retrying."
            ) from exc
        if now == saved:
            status: SaveStatus = "saved" if saved else "unsaved"
            return _result(url, job_id, saved, status)
        if listed:
            # Listed before and still listed: the click did not take.
            raise ActionOutcomeError(
                f"Job {job_id} was in the saved list, and still is after its "
                "Save button was clicked. Check get_saved_jobs before retrying."
            )
        # It was saved past the pages read, and the click unsaved it, or it was
        # unsaved and the click saved it: either way it is now the opposite of
        # what it was, so a second click puts it back as found. If that click
        # fails, the posting is left opposite to what was asked, and the error
        # has to say so rather than look like nothing happened.
        flipped = "unsaved" if saved else "saved"
        try:
            await self._toggle(url, job_id)
            again = job_id in await self._saved_job_ids()
        except Exception as exc:
            # The reason is the second attempt's; its "Nothing was clicked"
            # would read as if the first click never happened.
            reason = str(exc).removesuffix(" Nothing was clicked.").rstrip(".")
            raise ActionOutcomeError(
                f"Job {job_id} is now {flipped}, the opposite of what was "
                f"asked: telling its state apart took a click that {flipped} "
                f"it, and the click to put it back failed: {reason}. Call "
                f"{'save_job' if saved else 'unsave_job'} again, or check "
                "get_saved_jobs."
            ) from exc
        if again == saved:
            status = "already_saved" if saved else "already_unsaved"
            return _result(url, job_id, saved, status)
        raise ActionOutcomeError(
            f"Job {job_id}'s Save button was clicked twice, but the saved list "
            f"{'still does not hold it' if saved else 'still holds it'}. Check "
            "get_saved_jobs before retrying."
        )

    async def _read(self, job_id: str) -> dict[str, Any]:
        found = await self._session.page.evaluate(
            SAVE_BUTTON_JS,
            {
                "applyPath": f"/jobs/view/{job_id}/apply",
                "redirectPath": SAFETY_REDIRECT_PATH,
                "mark": _SAVE_MARK,
            },
        )
        if not isinstance(found, dict):
            raise LinkedInOperationError("The job posting could not be read")
        return found

    async def _toggle(self, url: str, job_id: str) -> None:
        """Open the posting and click its Save button once, confirmed by its text."""
        await self._session.delay(NAV_DELAY)
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        deadline = self._session.monotonic() + _POSTING_READY_TIMEOUT
        found = await self._read(job_id)
        while not found["found"] and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await self._read(job_id)
        if not found["found"]:
            raise LinkedInOperationError(
                f"The Save button of job {job_id} could not be found: the apply "
                f"link's row held {found['buttons']} buttons where one with an "
                "aria-label was expected, or the posting shows no apply link "
                "with one. Nothing was clicked."
            )
        before = found["text"]
        async with linkedin_element(
            self._session.page.locator(f"button[{_SAVE_MARK}]"), timeout=5000
        ) as button:
            await button.click(timeout=5000)
        deadline = self._session.monotonic() + _TOGGLE_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            now = await self._read(job_id)
            if now["found"] and now["text"] != before:
                return
        raise ActionOutcomeError(
            f"The Save button of job {job_id} was clicked, but did not change. "
            "Check get_saved_jobs before retrying."
        )


def _result(url: str, job_id: str, saved: bool, status: SaveStatus) -> dict[str, Any]:
    return {"url": url, "job_id": job_id, "saved": saved, "status": status}
