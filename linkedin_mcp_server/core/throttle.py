"""What LinkedIn answered with, kept for the length of one tool call.

A call that fails on something `raise_tool_error` does not classify reaches the
client as "Error calling tool", because the server masks unclassified errors.
When LinkedIn refused some of the requests the page made during that call, the
failure is most likely throttling, but nothing read a response status, so the
client was told nothing and the failure read as a parser bug.

So every response's status is recorded by a listener installed on the page at
browser start, and cleared as each tool call starts. The record is evidence
only: it raises nothing and retries nothing, and a call that succeeds is
untouched by it. `raise_tool_error` appends it to a failure that happened
anyway. Classifying a refused navigation as a rate limit is a separate
question, and is left to the navigation code.

Only the status, the URL and a numeric ``Retry-After`` are read. No body is
read and nothing is sent.

Global rather than per-page, like the browser it observes: one process drives
one shared page, and `raise_tool_error` is reached through call paths that
carry no record of their own.

Status 999 sits beside 429 because LinkedIn answers with it for the same
reason under a different name -- its "Request denied" for traffic it decides is
not a person.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary, WeakSet

from .bot_challenge import response_is_challenge
from .destination import is_linkedin_landing

logger = logging.getLogger(__name__)

THROTTLED_STATUSES = frozenset({429, 999})


@dataclass(frozen=True, slots=True)
class ThrottleHit:
    """One response LinkedIn refused to serve."""

    status: int
    url: str
    retry_after: int | None = None


_latest: ThrottleHit | None = None
_statuses: set[int] = set()
_count: int = 0
# This call's page loads LinkedIn refused, by path. A refused document is the
# one throttling a caller may act on: the page it asked for never came.
_refused_documents: dict[str, ThrottleHit] = {}
# Cloudflare's security checks this call met: how many, and the last URL.
_challenges: int = 0
_last_challenge: str | None = None
_watched: WeakSet[Any] = WeakSet()

# Which call each request was sent during. The record is cleared as a call
# starts, but a request the previous call sent can still be answered after
# that, and its refusal is no evidence about the call that is running now.
_call: int = 0
_sent_during: WeakKeyDictionary[Any, int] = WeakKeyDictionary()


def watch_responses(page: Any) -> None:
    """Record the status of every response this page receives."""
    try:
        if page in _watched:
            return
        _watched.add(page)
    except TypeError:
        # A page that cannot be weak-referenced is still worth watching; the
        # only cost of registering twice is a doubled count.
        logger.debug("Page is not weak-referenceable; watching it anyway")
    page.on("request", _note_request)
    page.on("response", _record)


# Whether the running task is a call that owns the browser. The record
# describes that call only: a browserless call such as get_session_status runs
# beside it, never clears the record, and must not report the refusals of the
# browser call it overlaps or follows.
_owns_the_browser: ContextVar[bool] = ContextVar("owns_the_browser", default=False)


@contextmanager
def browser_call_record() -> Generator[None]:
    """Start a fresh record for the browser call this block runs.

    Entered by the serializing middleware around the tool body, once it holds
    the browser, so only that call and what it awaits can read the record.
    """
    reset_throttle_record()
    token = _owns_the_browser.set(True)
    try:
        yield
    finally:
        _owns_the_browser.reset(token)


def reset_throttle_record() -> None:
    """Forget the previous call's evidence."""
    global _call, _count, _latest, _challenges, _last_challenge
    _call += 1
    _count = 0
    _latest = None
    _challenges = 0
    _last_challenge = None
    _statuses.clear()
    _refused_documents.clear()


def throttled_count() -> int:
    """How many responses were throttled during this call."""
    return _count


def throttle_evidence() -> str | None:
    """One sentence naming this call's throttling, or None when there was none.

    None outside a browser call, whose record belongs to another call.
    """
    last = _latest
    if not _owns_the_browser.get():
        return None
    challenged = (
        f"LinkedIn's bot protection (Cloudflare) answered {_challenges} "
        f"{'request' if _challenges == 1 else 'requests'} with a security "
        f"check during this call, most recently {_path(_last_challenge or '')}. "
        "Wait before retrying."
        if _challenges
        else None
    )
    if last is None:
        return challenged

    statuses = " and ".join(f"HTTP {status}" for status in sorted(_statuses))
    asked = (
        f" LinkedIn asked for {last.retry_after} seconds."
        if last.retry_after is not None
        else ""
    )
    return (
        f"LinkedIn answered {statuses} to {_count} "
        f"{'request' if _count == 1 else 'requests'} during this call, most "
        f"recently {_path(last.url)}. LinkedIn was throttling this session, "
        f"so wait before retrying.{asked}"
    ) + (f" {challenged}" if challenged else "")


def challenge_seen() -> bool:
    """Whether Cloudflare answered any request of this call with its check.

    False outside a browser call, whose record belongs to another call.
    """
    return _owns_the_browser.get() and _challenges > 0


def refused_document(path: str) -> ThrottleHit | None:
    """LinkedIn's throttled answer to a page load of *path* during this call.

    None outside a browser call, whose record belongs to another call. Only the
    document itself counts, never a request the page made, so a refused beacon
    beside a page that loaded is not mistaken for the page.
    """
    if not _owns_the_browser.get():
        return None
    return _refused_documents.get(path.rstrip("/") or "/")


def _path(url: str) -> str:
    """The path alone: a throttled URL's query carries ids and no diagnosis.

    Never the URL itself, which would bring the query along: a URL with no
    path is the root, and one that does not parse names nothing useful.
    """
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return "/"


def _retry_after(response: Any) -> int | None:
    """``Retry-After`` in seconds, when the header carries a plain number.

    The HTTP-date form is left unread: it is rare from LinkedIn, and a date
    parsed against a clock that may be wrong is worse than no answer.
    """
    try:
        raw = response.headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _note_request(request: Any) -> None:
    """Tag a request with the call it was sent during. Never raises."""
    try:
        _sent_during[request] = _call
    except TypeError:
        # Not weak-referenceable: its response is then taken as this call's,
        # which is what every response was before requests were tagged.
        pass


def _sent_before_this_call(response: Any) -> bool:
    """Whether the response answers a request an earlier call sent."""
    try:
        sent = _sent_during.get(response.request)
    except TypeError:
        return False
    return sent is not None and sent != _call


def _record(response: Any) -> None:
    """Note one response. Registered on the page; never raises into Playwright."""
    global _count, _latest, _challenges, _last_challenge

    try:
        status = int(response.status)
        challenged = response_is_challenge(response)
        if status not in THROTTLED_STATUSES and not challenged:
            return
        url = str(response.url)
        # Another host's 429 is no word from LinkedIn: a proxy or captive
        # portal answering for it would be misreported as LinkedIn's wait.
        if not is_linkedin_landing(url) or _sent_before_this_call(response):
            return
        if challenged and status not in THROTTLED_STATUSES:
            # A check, not a status LinkedIn refused with: told apart in the
            # evidence, and never counted as an HTTP 403 throttle.
            _challenges += 1
            _last_challenge = url
            return
        hit = ThrottleHit(status=status, url=url, retry_after=_retry_after(response))
    except Exception:
        logger.debug("Could not read a response status", exc_info=True)
        return

    _count += 1
    _latest = hit
    _statuses.add(status)
    try:
        document = getattr(response.request, "resource_type", None) == "document"
    except Exception:
        document = False
    if document:
        _refused_documents[urlsplit(url).path.rstrip("/") or "/"] = hit
