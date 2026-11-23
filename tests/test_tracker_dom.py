"""Browser-DOM tests for archiving and unarchiving a job in the job tracker.

``TRACKER_JOB_JS``, ``ARCHIVE_ITEM_JS`` and ``UNARCHIVE_DIALOG_JS`` never run
under the unit suite's mocked ``page.evaluate``. These drive
``TrackerActions`` against a synthetic tracker in headless chromium, with
navigation stubbed. The page carries the signals measured on 2026-11-23 and
nothing else: every job rendered twice, a hidden copy whose menu button's
componentkey ends ``_card_<id>`` and a shown one ending ``_table_<id>``; a
menu of ``role="menuitem"`` items each around an icon with an id; a Next
button around ``chevron-right-outline-small``; the unarchive dialog's radios,
Saved then In Progress, with no visible box of their own, and its Cancel and
Update buttons. Labels are German on purpose.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import (
    ActionOutcomeError,
    LinkedInOperationError,
)
from linkedin_mcp_server.linkedin import tracker_actions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.tracker_actions import TrackerActions

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

JOB = "4252026496"
PAGE_SIZE = 3

# A menu button opens its job's menu; an item records its choice on the body,
# which the test reads back. The unarchive item opens the stage dialog, whose
# Update records the radio chosen.
SCRIPT = """(() => {
const log = (what) => {
    const entries = JSON.parse(document.body.dataset.log || '[]');
    entries.push(what);
    document.body.dataset.log = JSON.stringify(entries);
};
const item = (icon, words, what) => '<div role="menuitem" data-what="' + what + '"><svg id="'
    + icon + '"></svg><span>' + words + '</span></div>';
for (const button of document.querySelectorAll('button[componentkey^="opportunityTrackerOverflowMenuButton_"]')) {
    button.addEventListener('click', () => {
        const key = button.getAttribute('componentkey');
        const id = key.split('_').pop();
        log('menu:' + key.split('_').slice(-2).join('_'));
        const archived = document.body.dataset.stage === 'archived';
        const menu = document.createElement('div');
        menu.setAttribute('role', 'menu');
        menu.innerHTML = (archived
            ? (document.body.hasAttribute('data-no-item') ? '' : item('box-archive-open-outline-medium', 'Archivierung aufheben', 'unarchive'))
            : item('arrow-up-down-outline-medium', 'Phase ändern', 'stage')
              + (document.body.hasAttribute('data-no-item') ? '' : item('box-archive-outline-medium', 'Archivieren', 'archive')))
            + item('trash-outline-medium', 'Löschen', 'delete');
        menu.addEventListener('click', (event) => {
            const what = event.target.closest('[role="menuitem"]')?.dataset.what;
            if (!what) return;
            log(what);
            menu.remove();
            if (what === 'archive' && !document.body.hasAttribute('data-ignore')) {
                document.body.dataset.change = 'archive:' + id;
            }
            if (what === 'unarchive') {
                const dialog = document.createElement('div');
                dialog.setAttribute('role', 'dialog');
                dialog.innerHTML = '<p>Phase wählen</p>'
                    + '<div><input type="radio" aria-label="Gespeichert" style="opacity:0;width:0;height:0;position:absolute"><span>Gespeichert</span></div>'
                    + '<div><input type="radio" aria-label="In Bearbeitung" style="opacity:0;width:0;height:0;position:absolute"><span>In Bearbeitung</span></div>'
                    + '<button type="button">Abbrechen</button><button type="button">Aktualisieren</button>';
                const [cancel, update] = dialog.querySelectorAll('button');
                cancel.addEventListener('click', () => dialog.remove());
                update.addEventListener('click', () => {
                    const radios = [...dialog.querySelectorAll('input')];
                    const chosen = radios.findIndex((r) => r.checked);
                    log('update:' + chosen);
                    if (chosen >= 0 && !document.body.hasAttribute('data-ignore')) {
                        document.body.dataset.change = 'unarchive:' + id + ':' + chosen;
                    }
                    dialog.remove();
                });
                document.body.appendChild(dialog);
            }
        });
        document.body.appendChild(menu);
    });
}
document.querySelector('button.next')?.addEventListener('click', () => log('next'));
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
        log('escape');
        for (const m of document.querySelectorAll('[role="menu"]')) m.remove();
    }
});
})();"""


def card(job_id: str, layout: str) -> str:
    return (
        f"<div><button type='button' aria-expanded='false' aria-label='Menü' "
        f"componentkey='opportunityTrackerOverflowMenuButton_{layout}_{job_id}'>"
        "<svg id='ellipses-horizontal-fill-small'></svg></button>"
        f"<a href='https://www.linkedin.com/jobs/view/{job_id}/'><p>Stelle {job_id}</p></a></div>"
    )


def tracker_page(stage: str, jobs: list[str], *, next_page: bool = False) -> str:
    hidden = "".join(card(j, "card") for j in jobs)
    shown = "".join(card(j, "table") for j in jobs)
    empty = (
        "<a href='https://www.linkedin.com/jobs/'>Mehr Jobs finden</a>"
        if not jobs
        else ""
    )
    nxt = (
        "<button class='next' type='button'><svg id='chevron-right-outline-small'></svg>Weiter</button>"
        if next_page
        else ""
    )
    return (
        f"<body data-stage='{stage}'><main>"
        f"<div style='display:none'>{hidden}</div><div>{shown}{empty}</div>{nxt}"
        f"</main><script>{SCRIPT}</script></body>"
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
    monkeypatch.setattr(tracker_actions, "_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(tracker_actions, "_MENU_TIMEOUT", 1.0)
    monkeypatch.setattr(tracker_actions, "_SETTLE", 0.1)
    monkeypatch.setattr(tracker_actions, "NAV_DELAY", 0.0)


class Tracker:
    """The tracker's stages between loads, paged PAGE_SIZE at a time."""

    def __init__(self, page, stages: dict[str, list[str]], body_attrs=()):
        self.page = page
        self.stages = {k: list(v) for k, v in stages.items()}
        self.body_attrs = body_attrs
        self.stage = ""
        self.offset = 0
        self.urls: list[str] = []
        self.log: list[str] = []

    async def collect(self) -> None:
        # Read once: what was read is cleared, so a later read sees only news.
        state = await self.page.evaluate(
            "() => { const b = document.body; if (!b) return ['[]', ''];"
            " const out = [b.dataset.log || '[]', b.dataset.change || ''];"
            " delete b.dataset.log; delete b.dataset.change; return out; }"
        )
        entries = json.loads(state[0])
        self.log.extend(entries)
        change = state[1]
        if change.startswith("archive:"):
            job = change.split(":")[1]
            for jobs in self.stages.values():
                if job in jobs:
                    jobs.remove(job)
            self.stages.setdefault("archived", []).insert(0, job)
        elif change.startswith("unarchive:"):
            _, job, chosen = change.split(":")
            self.stages["archived"].remove(job)
            target = ("saved", "in_progress")[int(chosen)]
            self.stages.setdefault(target, []).insert(0, job)
        if "next" in entries and self.stage:
            self.offset += PAGE_SIZE
            await self.render()

    async def render(self) -> None:
        jobs = self.stages.get(self.stage, [])
        shown = jobs[self.offset : self.offset + PAGE_SIZE]
        more = self.offset + PAGE_SIZE < len(jobs)
        await self.page.set_content(tracker_page(self.stage, shown, next_page=more))
        for name in self.body_attrs:
            await self.page.evaluate("(n) => document.body.setAttribute(n, '')", name)

    async def navigate(self, url: str) -> None:
        await self.collect()
        self.urls.append(url)
        self.stage = url.split("stage=")[1]
        self.offset = 0
        await self.render()


async def run(page, tracker: Tracker, method: str, *args):
    await page.goto("https://www.linkedin.com/jobs-tracker/")
    session = PageSession(page)
    actions = TrackerActions(session, PageNavigator(session))
    real_click = actions._click

    async def click(selector: str, **kwargs):
        await real_click(selector, **kwargs)
        await tracker.collect()

    with (
        patch.object(
            actions._navigator, "_navigate_to_page", side_effect=tracker.navigate
        ),
        patch.object(actions, "_click", side_effect=click),
        patch.object(PageSession, "check_rate_limit", new_callable=AsyncMock),
    ):
        try:
            return await getattr(actions, method)(*args)
        finally:
            await tracker.collect()


async def test_archiving_chooses_the_shown_copys_archive_item(dom_page):
    tracker = Tracker(dom_page, {"applied": ["111", JOB, "222"]})

    result = await run(dom_page, tracker, "archive_job", JOB, "applied")

    assert result == {
        "url": f"https://www.linkedin.com/jobs/view/{JOB}",
        "job_id": JOB,
        "stage": "applied",
        "status": "archived",
    }
    # Only the shown copy's button, never the hidden layout's.
    assert tracker.log == [f"menu:table_{JOB}", "archive"]
    assert tracker.stages["archived"] == [JOB]
    assert tracker.urls[-1].endswith("stage=archived")


async def test_a_job_on_a_later_page_is_found_with_next(dom_page):
    jobs = [str(1000 + i) for i in range(5)] + [JOB]
    tracker = Tracker(dom_page, {"saved": jobs})

    result = await run(dom_page, tracker, "archive_job", JOB, "saved")

    assert result["status"] == "archived"
    assert tracker.log == ["next", f"menu:table_{JOB}", "archive"]


async def test_the_pages_read_are_bounded(dom_page):
    jobs = [str(1000 + i) for i in range(6)] + [JOB]
    tracker = Tracker(dom_page, {"saved": jobs, "archived": []})

    with pytest.raises(LinkedInOperationError, match="first 2 pages"):
        await run(dom_page, tracker, "archive_job", JOB, "saved", 2)
    assert "archive" not in tracker.log


async def test_an_archived_job_is_left_alone(dom_page):
    tracker = Tracker(dom_page, {"applied": ["111"], "archived": ["222", JOB]})

    result = await run(dom_page, tracker, "archive_job", JOB, "applied")

    assert result["status"] == "already_archived"
    assert tracker.log == []


async def test_a_job_in_neither_stage_is_an_error_with_nothing_chosen(dom_page):
    tracker = Tracker(dom_page, {"applied": ["111"], "archived": []})

    with pytest.raises(LinkedInOperationError, match="Nothing was archived"):
        await run(dom_page, tracker, "archive_job", JOB, "applied")
    assert tracker.log == []


async def test_a_menu_without_the_archive_item_is_closed(dom_page):
    tracker = Tracker(dom_page, {"applied": [JOB]}, body_attrs=["data-no-item"])

    with pytest.raises(LinkedInOperationError, match="no single archive item"):
        await run(dom_page, tracker, "archive_job", JOB, "applied")
    assert tracker.log == [f"menu:table_{JOB}", "escape"]


async def test_an_archive_linkedin_does_not_keep_is_an_unknown_outcome(dom_page):
    tracker = Tracker(
        dom_page, {"applied": [JOB], "archived": []}, body_attrs=["data-ignore"]
    )

    with pytest.raises(ActionOutcomeError, match="does not list it"):
        await run(dom_page, tracker, "archive_job", JOB, "applied")


@pytest.mark.parametrize(("stage", "index"), [("saved", 0), ("in_progress", 1)])
async def test_unarchiving_chooses_the_stage_by_its_place(dom_page, stage, index):
    tracker = Tracker(dom_page, {"archived": ["111", JOB]})

    result = await run(dom_page, tracker, "unarchive_job", JOB, stage)

    assert result["status"] == "unarchived"
    assert tracker.log == [f"menu:table_{JOB}", "unarchive", f"update:{index}"]
    assert tracker.stages[stage] == [JOB]
    assert tracker.stages["archived"] == ["111"]


async def test_a_job_not_archived_is_already_unarchived(dom_page):
    tracker = Tracker(dom_page, {"archived": ["111"]})

    result = await run(dom_page, tracker, "unarchive_job", JOB, "saved")

    assert result["status"] == "already_unarchived"
    assert tracker.log == []


async def test_an_unarchive_linkedin_does_not_keep_is_an_unknown_outcome(dom_page):
    tracker = Tracker(dom_page, {"archived": [JOB]}, body_attrs=["data-ignore"])

    with pytest.raises(ActionOutcomeError, match="still lists it"):
        await run(dom_page, tracker, "unarchive_job", JOB, "saved")


async def test_an_archived_menu_without_unarchive_is_closed(dom_page):
    tracker = Tracker(dom_page, {"archived": [JOB]}, body_attrs=["data-no-item"])

    with pytest.raises(LinkedInOperationError, match="no single unarchive item"):
        await run(dom_page, tracker, "unarchive_job", JOB, "saved")
    assert tracker.log == [f"menu:table_{JOB}", "escape"]
