"""The signed-in member's job alerts: list them, create one, delete one.

Measured 2026-11-23, English locale, by creating one alert from a job search,
reading the alert manager with it, and deleting it from the manager:

- `/jobs/jam/` opens the alert manager as a dialog (`[data-test-modal]`). Each
  alert is one `li` around its edit button (`button[id^="editAlertIndex__"]`),
  with the alert's search as a link (`a[href*="/jobs/search"]`, its keywords
  as the link's text), the location in the element after the link's own, and
  its frequency on the line below. The dialog always carries the "Top job
  picks" switch (`input[data-artdeco-toggle-button]`), with or without
  alerts, which tells a rendered empty manager from one still loading.
- A job search page carries the alert switch for its search as
  `input[role="switch"]` inside `[componentkey^="JobSearchAlertToggleRef"]`,
  whose box and label have no size: it is turned on with a DOM click, which
  LinkedIn answers with a "job alert was created" toast. A reloaded search
  page showed the switch off again while the alert existed, so the switch is
  never read as the state: the manager is.
- The edit dialog an alert's edit button opens has the alert's save button
  (`#manage-alert-save`) and, beside the element holding it, its delete
  button. Delete asks for a confirmation whose own action is
  `button[data-test-dialog-primary-btn]`.

Ids, attributes and structure, never words.

Verified live 2026-11-23 through the MCP tools: create, create again
(already_exists), list, delete, delete again (already_deleted).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from linkedin_mcp_server.core.destination import linkedin_element
from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.search_urls import build_job_search_url
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

logger = logging.getLogger(__name__)

AlertStatus = Literal["created", "deleted", "already_exists", "already_deleted"]

JOB_ALERTS_URL = "https://www.linkedin.com/jobs/jam/"
SECTION = "job_alerts"
_EDIT_MARK = "data-mcp-alert-edit"
_SWITCH_MARK = "data-mcp-alert-switch"
_DELETE_MARK = "data-mcp-alert-delete"
_MANAGER_TIMEOUT = 12.0
_SEARCH_READY_TIMEOUT = 12.0
_DIALOG_TIMEOUT = 6.0
_POLL = 0.25

# The alert manager: null while it has not rendered, else its alerts and text.
ALERTS_JS = r"""() => {
    const dialog = [...document.querySelectorAll('[data-test-modal], [role="dialog"]')]
        .find((d) => d.querySelector('input[data-artdeco-toggle-button], button[id^="editAlertIndex__"]'));
    if (!dialog) return null;
    const alerts = [...dialog.querySelectorAll('button[id^="editAlertIndex__"]')].map((edit) => {
        // The alert's row: its list item, else the widest ancestor that holds
        // this edit button alone.
        let row = edit.closest('li');
        if (!row) {
            row = edit;
            while (row.parentElement && row.parentElement !== dialog
                   && row.parentElement.querySelectorAll('button[id^="editAlertIndex__"]').length === 1) {
                row = row.parentElement;
            }
        }
        const link = row.querySelector('a[href*="/jobs/search"]');
        const words = (el) => (el ? (el.innerText || el.textContent || '') : '')
            .replace(/\s+/g, ' ').trim();
        // The location is the element after the one holding the link.
        let holder = link;
        while (holder && holder.parentElement && !holder.nextElementSibling) {
            holder = holder.parentElement;
        }
        const location = holder && holder.nextElementSibling;
        const keywords = words(link);
        const lines = (row.innerText || '').split('\n').map((l) => l.trim()).filter(Boolean);
        return {
            edit: edit.id,
            keywords,
            location: words(location),
            search_url: link ? link.href : '',
            details: lines.filter((l) => l !== keywords && l !== words(location)),
        };
    });
    return {alerts, text: dialog.innerText || ''};
}"""

# The search page's alert switch: marked, with whether it shows on.
ALERT_SWITCH_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) old.removeAttribute(mark);
    const switches = [...document.querySelectorAll(
        '[componentkey^="JobSearchAlertToggleRef"] input[role="switch"]'
    )];
    if (switches.length !== 1) return {switches: switches.length, on: null};
    switches[0].setAttribute(mark, '');
    return {switches: 1, on: switches[0].checked};
}"""

# The edit dialog's delete button: beside the element holding its save button.
ALERT_DELETE_JS = r"""(mark) => {
    for (const old of document.querySelectorAll('[' + mark + ']')) old.removeAttribute(mark);
    const save = document.getElementById('manage-alert-save');
    if (!save || !save.parentElement || !save.parentElement.parentElement) return 0;
    const footer = save.parentElement.parentElement;
    const own = [...footer.children].filter((el) => el.tagName === 'BUTTON');
    if (own.length !== 1) return own.length;
    own[0].setAttribute(mark, '');
    return 1;
}"""

_CONFIRM_SELECTOR = "button[data-test-dialog-primary-btn]"


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def matching(
    alerts: list[dict[str, Any]], keywords: str, location: str | None
) -> list[dict[str, Any]]:
    """The alerts for *keywords*, and at *location* when one is given.

    Keywords match whole, ignoring case and spacing. LinkedIn writes the
    location out ("Berkeley, California, United States (within 25 miles)"),
    so a given location matches the start of it.
    """
    want = _norm(keywords)
    found = [alert for alert in alerts if _norm(alert.get("keywords")) == want]
    if location:
        place = _norm(location)
        found = [
            alert for alert in found if _norm(alert.get("location")).startswith(place)
        ]
    return found


def _public(alert: dict[str, Any]) -> dict[str, Any]:
    return {
        "keywords": alert.get("keywords") or "",
        "location": alert.get("location") or "",
        "details": alert.get("details") or [],
        "search_url": alert.get("search_url") or "",
    }


class JobAlertActions:
    """Read, create and delete the signed-in member's job alerts."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def read_job_alerts(self) -> dict[str, Any]:
        """List the job alerts from LinkedIn's alert manager.

        Returns:
            {url, sections: {job_alerts: text}, job_alerts: [{keywords,
            location, details, search_url}]}.
        """
        found = await self._manager()
        return {
            "url": JOB_ALERTS_URL,
            "sections": {SECTION: found["text"]},
            SECTION: [_public(alert) for alert in found["alerts"]],
        }

    async def set_job_alert(
        self, keywords: str, location: str | None, on: bool
    ) -> dict[str, Any]:
        """Create or delete the alert for one search, leaving it as asked.

        The manager is read first, so an alert that already exists is not made
        twice and one that is gone is not looked for again. Either change is
        confirmed by reading the manager again.

        Returns:
            {keywords, location, status, alert} where status is "created",
            "deleted", "already_exists" or "already_deleted", and alert is the
            manager's entry (null once deleted).
        """
        keywords = " ".join(keywords.split())
        if not keywords:
            raise LinkedInOperationError("A job alert needs the search's keywords.")
        found = matching((await self._manager())["alerts"], keywords, location)
        if on and found:
            return _result(keywords, location, "already_exists", found[0])
        if not on and not found:
            return _result(keywords, location, "already_deleted", None)
        if on:
            await self._turn_on(keywords, location)
        else:
            if len(found) > 1:
                raise LinkedInOperationError(
                    f"{len(found)} job alerts are for {keywords!r}"
                    + (f" at {location!r}" if location else "")
                    + "; give the location as get_job_alerts shows it to pick "
                    "one. Nothing was deleted."
                )
            await self._delete(found[0])
        await self._session.delay(NAV_DELAY)
        try:
            after = matching((await self._manager())["alerts"], keywords, location)
        except LinkedInOperationError as exc:
            if type(exc) is not LinkedInOperationError:
                raise
            raise ActionOutcomeError(
                f"The job alert for {keywords!r} was "
                f"{'turned on' if on else 'deleted'}, but the alert manager "
                f"could not be read afterwards ({exc}). Read get_job_alerts "
                "before retrying."
            ) from exc
        if on and not after:
            raise ActionOutcomeError(
                f"The job alert switch for {keywords!r} was turned on, but the "
                "alert manager does not list it, so LinkedIn may not have kept "
                "it. Calling this again is safe: it reads the manager first."
            )
        if not on and after:
            raise ActionOutcomeError(
                f"The job alert for {keywords!r} was deleted and confirmed, but "
                "the alert manager still lists it. Calling this again is safe: "
                "it reads the manager first."
            )
        if on:
            return _result(keywords, location, "created", after[0])
        return _result(keywords, location, "deleted", None)

    async def _manager(self) -> dict[str, Any]:
        """Open the alert manager and read it once it has rendered.

        Its dialog is the page itself, so no modal is dismissed here.
        """
        page = self._session.page
        await self._navigator._navigate_to_page(JOB_ALERTS_URL)
        await self._session.check_rate_limit()
        deadline = self._session.monotonic() + _MANAGER_TIMEOUT
        found = await page.evaluate(ALERTS_JS)
        while found is None and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await page.evaluate(ALERTS_JS)
        if found is None:
            raise LinkedInOperationError(
                f"{JOB_ALERTS_URL} did not show LinkedIn's job alert manager."
            )
        return found

    async def _turn_on(self, keywords: str, location: str | None) -> None:
        page = self._session.page
        url = build_job_search_url(keywords, location)
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        deadline = self._session.monotonic() + _SEARCH_READY_TIMEOUT
        found = await page.evaluate(ALERT_SWITCH_JS, _SWITCH_MARK)
        while found["switches"] != 1 and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            found = await page.evaluate(ALERT_SWITCH_JS, _SWITCH_MARK)
        if found["switches"] != 1:
            raise LinkedInOperationError(
                f"The job search for {keywords!r} showed {found['switches']} job "
                "alert switches where one was expected. Nothing was turned on."
            )
        if found["on"]:
            # Shown on although the manager lists no such alert: clicking would
            # turn something off.
            raise LinkedInOperationError(
                f"The job search for {keywords!r} shows its alert on, but the "
                "alert manager lists none for it. Nothing was clicked."
            )
        async with linkedin_element(
            page.locator(f"input[{_SWITCH_MARK}]"), timeout=5000
        ) as element:
            # The switch's box has no size, so no pointer can reach it.
            await element.evaluate("(el) => el.click()")

    async def _delete(self, alert: dict[str, Any]) -> None:
        page = self._session.page
        await page.evaluate(
            "([id, mark]) => { const el = document.getElementById(id);"
            " if (el) el.setAttribute(mark, ''); }",
            [alert["edit"], _EDIT_MARK],
        )
        await self._click(f"button[{_EDIT_MARK}]")
        deadline = self._session.monotonic() + _DIALOG_TIMEOUT
        count = await page.evaluate(ALERT_DELETE_JS, _DELETE_MARK)
        while count != 1 and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
            count = await page.evaluate(ALERT_DELETE_JS, _DELETE_MARK)
        if count != 1:
            await page.keyboard.press("Escape")
            raise LinkedInOperationError(
                f"The edit dialog of the job alert for {alert['keywords']!r} "
                f"showed {count} delete buttons where one was expected. It was "
                "closed and nothing was deleted."
            )
        await self._click(f"button[{_DELETE_MARK}]")
        confirm = page.locator(_CONFIRM_SELECTOR)
        deadline = self._session.monotonic() + _DIALOG_TIMEOUT
        while await confirm.count() != 1 and self._session.monotonic() < deadline:
            await self._session.delay(_POLL)
        if await confirm.count() != 1:
            await page.keyboard.press("Escape")
            raise LinkedInOperationError(
                f"Deleting the job alert for {alert['keywords']!r} asked for no "
                "confirmation LinkedIn's way. It was closed and nothing was "
                "deleted."
            )
        await self._click(_CONFIRM_SELECTOR)

    async def _click(self, selector: str) -> None:
        async with linkedin_element(
            self._session.page.locator(selector), timeout=5000
        ) as element:
            await element.click(timeout=5000)


def _result(
    keywords: str,
    location: str | None,
    status: AlertStatus,
    alert: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "keywords": keywords,
        "location": location,
        "status": status,
        "alert": _public(alert) if alert else None,
    }
