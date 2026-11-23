"""Browser-DOM tests for listing, creating and deleting job alerts.

``ALERTS_JS``, ``ALERT_SWITCH_JS`` and ``ALERT_DELETE_JS`` never run under the
unit suite's mocked ``page.evaluate``. These drive ``JobAlertActions`` against
synthetic pages in headless chromium, with navigation stubbed: the alert
manager LinkedIn opens on ``/jobs/jam/`` and a job search page with its alert
switch. They carry the signals measured on 2026-11-23 and nothing else: the
manager as ``[data-test-modal]`` with its "top job picks" switch, one ``li``
per alert around ``button[id^="editAlertIndex__"]``, the search link and the
location beside it; the search's switch inside
``[componentkey^="JobSearchAlertToggleRef"]`` with no box of its own; the edit
dialog's ``#manage-alert-save`` with the delete button beside its holder; the
confirmation's ``data-test-dialog-primary-btn``. Labels are German on purpose.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import html
from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin import job_alerts
from linkedin_mcp_server.linkedin.job_alerts import JobAlertActions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

BERKELEY = "Berkeley, California, Vereinigte Staaten (im Umkreis von 40 km)"

# The manager's edit buttons open the edit dialog; its delete button opens the
# confirmation, whose primary button records the deletion on the body (the
# test reads it back), unless the body says to ignore it.
MANAGER_SCRIPT = """(() => {
const log = (what) => {
    const entries = JSON.parse(document.body.dataset.log || '[]');
    entries.push(what);
    document.body.dataset.log = JSON.stringify(entries);
};
for (const edit of document.querySelectorAll('button[id^="editAlertIndex__"]')) {
    edit.addEventListener('click', () => {
        log('edit:' + edit.id);
        const dialog = document.createElement('div');
        dialog.setAttribute('data-test-modal', '');
        dialog.setAttribute('role', 'dialog');
        dialog.innerHTML = '<h2>Jobalert bearbeiten</h2>'
            + '<button data-test-modal-close-btn aria-label="Schließen"><svg data-test-icon="close-medium"></svg></button>'
            + '<div class="footer"><button class="delete">Jobalert löschen</button>'
            + '<div><button>Zurück</button><button id="manage-alert-save">Speichern</button></div></div>';
        dialog.querySelector('.delete').addEventListener('click', () => {
            log('delete');
            const confirm = document.createElement('div');
            confirm.setAttribute('role', 'alertdialog');
            confirm.innerHTML = '<p>Jobalert löschen?</p>'
                + '<button data-test-dialog-secondary-btn>Abbrechen</button>'
                + '<button data-test-dialog-primary-btn>Löschen</button>';
            confirm.querySelector('[data-test-dialog-primary-btn]').addEventListener('click', () => {
                log('confirm');
                if (!document.body.hasAttribute('data-ignore')) {
                    document.body.dataset.deleted = edit.dataset.index;
                }
                confirm.remove();
                dialog.remove();
            });
            document.body.appendChild(confirm);
        });
        document.body.appendChild(dialog);
    });
}
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') log('escape');
});
})();"""

SEARCH_SCRIPT = """(() => {
const box = document.querySelector('input[role="switch"]');
box?.addEventListener('change', () => {
    const entries = JSON.parse(document.body.dataset.log || '[]');
    entries.push('switch:' + box.checked);
    document.body.dataset.log = JSON.stringify(entries);
    if (box.checked && !document.body.hasAttribute('data-ignore')) {
        document.body.dataset.created = '1';
    }
});
})();"""


def alert_row(index: int, keywords: str, location: str) -> str:
    return (
        "<li><div><div><svg data-test-icon='briefcase-jobs'></svg><div><div>"
        f"<a href='https://www.linkedin.com/jobs/search-results/?keywords={html.escape(keywords)}'>"
        f"{html.escape(keywords)}</a></div><span>{html.escape(location)}</span></div></div>"
        f"<div><button id='editAlertIndex__{index}' data-index='{index}' "
        f"aria-label='Bearbeiten'><svg data-test-icon='edit-medium'></svg></button></div></div>"
        "<div><div>Häufigkeit: Täglich per E-Mail und Benachrichtigung</div></div></li>"
    )


def manager(alerts: list[tuple[str, str]], *, rendered: bool = True) -> str:
    if not rendered:
        return "<main><p>Lädt …</p></main>"
    rows = "".join(alert_row(i, k, loc) for i, (k, loc) in enumerate(alerts))
    body = (
        f"<ul>{rows}</ul>"
        if alerts
        else "<section><h2>Noch keine Jobalerts</h2></section>"
    )
    return (
        "<main></main><div data-test-modal role='dialog'><h2>Jobalerts verwalten</h2>"
        # As measured: the list and the "top job picks" switch share one
        # container below the dialog.
        f"<div><div>{body}</div><hr><div><h3>Top-Jobs für Sie</h3>"
        "<input role='switch' type='checkbox' "
        "data-artdeco-toggle-button='true' checked></div></div>"
        f"<button>Fertig</button></div><script>{MANAGER_SCRIPT}</script>"
    )


def search_page(*, switches: int = 1, on: bool = False) -> str:
    one = (
        "<div componentkey='JobSearchAlertToggleRef_data_engineer_101_0'><div><p></p></div>"
        "<div><div><label for='s'></label><input role='switch' id='s' "
        "aria-label='Einstellung umschalten' type='checkbox'"
        + (" checked" if on else "")
        + " style='position:absolute;width:0;height:0;opacity:0'><span></span></div></div></div>"
    )
    return (
        "<main><p>99+ Ergebnisse</p>" + one * switches + "</main>"
        f"<script>{SEARCH_SCRIPT}</script>"
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
    monkeypatch.setattr(job_alerts, "_MANAGER_TIMEOUT", 1.0)
    monkeypatch.setattr(job_alerts, "_SEARCH_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(job_alerts, "_DIALOG_TIMEOUT", 1.0)
    monkeypatch.setattr(job_alerts, "NAV_DELAY", 0.0)


class Site:
    """LinkedIn as far as these pages go: the alerts kept between loads."""

    def __init__(self, page, alerts, *, search=None, body_attrs=(), rendered=True):
        self.page = page
        self.alerts = list(alerts)
        self.search = search or search_page()
        self.body_attrs = body_attrs
        self.rendered = rendered
        self.urls: list[str] = []
        self.log: list[str] = []

    async def _collect(self) -> None:
        state = await self.page.evaluate(
            "() => [document.body?.dataset.log || '[]', document.body?.dataset.created || '',"
            " document.body?.dataset.deleted || '']"
        )
        import json

        self.log.extend(json.loads(state[0]))
        if state[1]:
            self.alerts.append(("data engineer", BERKELEY))
        if state[2]:
            del self.alerts[int(state[2])]

    async def navigate(self, url: str) -> None:
        await self._collect()
        self.urls.append(url)
        content = (
            manager(self.alerts, rendered=self.rendered)
            if url == job_alerts.JOB_ALERTS_URL
            else self.search
        )
        await self.page.set_content(content)
        for name in self.body_attrs:
            await self.page.evaluate("(n) => document.body.setAttribute(n, '')", name)


async def run(page, site: Site, method: str, *args):
    await page.goto("https://www.linkedin.com/feed/")
    session = PageSession(page)
    actions = JobAlertActions(session, PageNavigator(session))
    with (
        patch.object(
            actions._navigator, "_navigate_to_page", side_effect=site.navigate
        ),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
        patch.object(PageSession, "dismiss_modal", new_callable=AsyncMock),
    ):
        try:
            return await getattr(actions, method)(*args)
        finally:
            await site._collect()


async def test_the_alerts_are_read_with_their_search_location_and_frequency(
    dom_page,
):
    site = Site(dom_page, [("data engineer", BERKELEY), ("Datenanalyst", "Berlin")])

    result = await run(dom_page, site, "read_job_alerts")

    assert result["url"] == job_alerts.JOB_ALERTS_URL
    assert result["job_alerts"] == [
        {
            "keywords": "data engineer",
            "location": BERKELEY,
            "details": ["Häufigkeit: Täglich per E-Mail und Benachrichtigung"],
            "search_url": "https://www.linkedin.com/jobs/search-results/?keywords=data%20engineer",
        },
        {
            "keywords": "Datenanalyst",
            "location": "Berlin",
            "details": ["Häufigkeit: Täglich per E-Mail und Benachrichtigung"],
            "search_url": "https://www.linkedin.com/jobs/search-results/?keywords=Datenanalyst",
        },
    ]
    assert "Jobalerts verwalten" in result["sections"]["job_alerts"]


async def test_a_lone_alert_keeps_only_its_own_lines(dom_page):
    # One alert: its row is its list item, not everything up to the dialog.
    site = Site(dom_page, [("data engineer", BERKELEY)])

    result = await run(dom_page, site, "read_job_alerts")

    assert result["job_alerts"][0]["details"] == [
        "Häufigkeit: Täglich per E-Mail und Benachrichtigung"
    ]


async def test_an_empty_manager_is_an_empty_list(dom_page):
    result = await run(dom_page, Site(dom_page, []), "read_job_alerts")

    assert result["job_alerts"] == []


async def test_a_manager_that_never_renders_is_an_error(dom_page):
    with pytest.raises(LinkedInOperationError, match="alert manager"):
        await run(dom_page, Site(dom_page, [], rendered=False), "read_job_alerts")


async def test_creating_turns_on_the_searchs_switch_and_reads_the_manager_again(
    dom_page,
):
    site = Site(dom_page, [])

    result = await run(dom_page, site, "set_job_alert", "Data  Engineer", None, True)

    assert result["status"] == "created"
    assert result["alert"]["location"] == BERKELEY
    assert site.log == ["switch:true"]
    assert site.urls[0] == job_alerts.JOB_ALERTS_URL
    assert "jobs/search/?keywords=Data+Engineer" in site.urls[1]
    assert site.urls[2] == job_alerts.JOB_ALERTS_URL


async def test_an_alert_that_exists_is_not_made_twice(dom_page):
    site = Site(dom_page, [("data engineer", BERKELEY)])

    result = await run(
        dom_page, site, "set_job_alert", "DATA ENGINEER", "berkeley", True
    )

    assert result["status"] == "already_exists"
    assert site.log == []
    assert site.urls == [job_alerts.JOB_ALERTS_URL]


async def test_an_alert_elsewhere_is_not_the_one_asked_for(dom_page):
    site = Site(dom_page, [("data engineer", "Berlin")])

    result = await run(
        dom_page, site, "set_job_alert", "data engineer", "Berkeley", True
    )

    assert result["status"] == "created"
    assert site.log == ["switch:true"]


async def test_a_switch_linkedin_does_not_keep_is_an_unknown_outcome(dom_page):
    site = Site(dom_page, [], body_attrs=["data-ignore"])

    with pytest.raises(ActionOutcomeError, match="does not list it"):
        await run(dom_page, site, "set_job_alert", "data engineer", None, True)


async def test_a_switch_shown_on_is_never_clicked(dom_page):
    site = Site(dom_page, [], search=search_page(on=True))

    with pytest.raises(LinkedInOperationError, match="Nothing was clicked") as caught:
        await run(dom_page, site, "set_job_alert", "data engineer", None, True)
    assert type(caught.value) is LinkedInOperationError
    assert site.log == []


async def test_two_switches_are_never_guessed_between(dom_page):
    site = Site(dom_page, [], search=search_page(switches=2))

    with pytest.raises(LinkedInOperationError, match="2 job alert switches"):
        await run(dom_page, site, "set_job_alert", "data engineer", None, True)
    assert site.log == []


async def test_deleting_confirms_linkedins_dialog_and_reads_the_manager_again(
    dom_page,
):
    site = Site(dom_page, [("Datenanalyst", "Berlin"), ("data engineer", BERKELEY)])

    result = await run(dom_page, site, "set_job_alert", "data engineer", None, False)

    assert result == {
        "keywords": "data engineer",
        "location": None,
        "status": "deleted",
        "alert": None,
    }
    assert site.log == ["edit:editAlertIndex__1", "delete", "confirm"]
    assert site.alerts == [("Datenanalyst", "Berlin")]


async def test_an_alert_that_is_gone_is_already_deleted(dom_page):
    site = Site(dom_page, [("Datenanalyst", "Berlin")])

    result = await run(dom_page, site, "set_job_alert", "data engineer", None, False)

    assert result["status"] == "already_deleted"
    assert site.log == []


async def test_two_alerts_for_the_same_keywords_need_the_location(dom_page):
    site = Site(dom_page, [("data engineer", "Berlin"), ("data engineer", BERKELEY)])

    with pytest.raises(LinkedInOperationError, match="2 job alerts"):
        await run(dom_page, site, "set_job_alert", "data engineer", None, False)
    assert site.log == []

    result = await run(
        dom_page, site, "set_job_alert", "data engineer", "Berkeley", False
    )
    assert result["status"] == "deleted"
    assert site.alerts == [("data engineer", "Berlin")]


async def test_a_deletion_linkedin_does_not_keep_is_an_unknown_outcome(dom_page):
    site = Site(dom_page, [("data engineer", BERKELEY)], body_attrs=["data-ignore"])

    with pytest.raises(ActionOutcomeError, match="still lists it"):
        await run(dom_page, site, "set_job_alert", "data engineer", None, False)
    assert site.log == ["edit:editAlertIndex__0", "delete", "confirm"]


async def test_blank_keywords_never_open_anything(dom_page):
    site = Site(dom_page, [])

    with pytest.raises(LinkedInOperationError, match="keywords"):
        await run(dom_page, site, "set_job_alert", "  ", None, True)
    assert site.urls == []
