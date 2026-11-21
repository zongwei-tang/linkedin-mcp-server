"""Keep a crashed owner's profile lease until marked browser groups are gone."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

_BROWSER_PROCESS_MARKER = "LINKEDIN_MCP_BROWSER_PROCESS_MARKER"
_POLL_SECONDS = 0.01
_QUIET_SECONDS = 1.0
#: The first wait after a scan that could not run, doubled after each further
#: one up to the cap. A scan that fails fails at once (a missing or forbidden
#: ``ps``, a timeout), so retrying it every _POLL_SECONDS is a fork and an exec
#: a hundred times a second, forever, in every orphaned guardian: measured
#: 2026-11-21 as 43 of them holding a Mac's CPU at 53-61% system time inside a
#: sandbox that refuses to run ``ps`` at all.
_BLIND_POLL_SECONDS = 0.5
_BLIND_POLL_CAP_SECONDS = 5.0
#: How long scans may keep failing before the guardian gives up and exits. It
#: can neither see nor kill anything while blind, so holding on longer only
#: keeps the lease and the load, and the load is what keeps a loaded host's
#: ``ps`` failing.
_BLIND_LIMIT_SECONDS = 600.0
#: How long one ``ps`` may take outside Linux. It prints every process's
#: environment, which is slow on a Mac even without load; see the process scan
#: entries in docs/daemon-measurements.md. A scan cut off by this bound proves
#: nothing and is asked again, so a bound that a loaded host's ``ps`` keeps
#: exceeding holds the profile and leaves the browser running for as long as
#: the load lasts, up to _BLIND_LIMIT_SECONDS. The bound only has to stop a
#: ``ps`` that never returns.
_SNAPSHOT_SECONDS = 5.0


def _linux_groups(markers: set[str]) -> set[int] | None:
    groups: set[int] = set()
    expected = {f"{_BROWSER_PROCESS_MARKER}={marker}".encode() for marker in markers}
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            environment = set((entry / "environ").read_bytes().split(b"\0"))
            raw = (entry / "stat").read_text()
        except (OSError, UnicodeError):
            continue
        if environment.isdisjoint(expected):
            continue
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split() if closing >= 0 else []
        if len(fields) <= 2:
            continue
        try:
            groups.add(int(fields[2]))
        except ValueError:
            continue
    return groups


def _ps_groups(markers: set[str]) -> set[int] | None:
    ps = next(
        (
            candidate
            for candidate in ("/bin/ps", "/usr/bin/ps")
            if Path(candidate).is_file()
        ),
        None,
    )
    if ps is None:
        return None
    try:
        snapshot = subprocess.run(
            [ps, "eww", "-A", "-o", "pid=", "-o", "pgid=", "-o", "command="],
            check=True,
            capture_output=True,
            timeout=_SNAPSHOT_SECONDS,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    needles = {
        f"{_BROWSER_PROCESS_MARKER}={marker}".encode() + b" " for marker in markers
    }
    groups: set[int] = set()
    for line in snapshot.splitlines():
        fields = line.lstrip().split(maxsplit=2)
        if len(fields) != 3 or all(
            needle not in fields[2] + b" " for needle in needles
        ):
            continue
        try:
            groups.add(int(fields[1]))
        except ValueError:
            continue
    return groups


def _marked_groups(markers: set[str]) -> set[int] | None:
    if not markers:
        return set()
    if sys.platform.startswith("linux"):
        return _linux_groups(markers)
    return _ps_groups(markers)


def _drain(markers: set[str]) -> None:
    own_group = os.getpgrp()
    quiet_since: float | None = None
    blind_since: float | None = None
    blind_wait = _BLIND_POLL_SECONDS
    while True:
        groups = _marked_groups(markers)
        if groups is None:
            quiet_since = None
            now = time.monotonic()
            if blind_since is None:
                blind_since = now
            elif now - blind_since >= _BLIND_LIMIT_SECONDS:
                return
            time.sleep(blind_wait)
            blind_wait = min(blind_wait * 2, _BLIND_POLL_CAP_SECONDS)
            continue
        blind_since = None
        blind_wait = _BLIND_POLL_SECONDS
        groups.discard(own_group)
        if groups:
            quiet_since = None
            for group in groups:
                try:
                    os.killpg(group, signal.SIGKILL)
                except OSError:
                    pass
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= _QUIET_SECONDS:
            return
        time.sleep(_POLL_SECONDS)


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    control_fd = int(sys.argv[1])
    ready_fd = int(sys.argv[2])
    owner_group = int(sys.argv[3])
    markers: set[str] = set()
    with os.fdopen(control_fd, "rb", closefd=True) as control:
        os.write(ready_fd, b"ready\n")
        os.close(ready_fd)
        for raw in control:
            line = raw.decode("ascii", "ignore").strip()
            if line == "release":
                return 0
            prefix = "marker "
            if line.startswith(prefix):
                marker = line.removeprefix(prefix)
                if len(marker) == 64:
                    markers.add(marker)
    if owner_group > 0 and owner_group != os.getpgrp():
        try:
            os.killpg(owner_group, signal.SIGKILL)
        except OSError:
            pass
    _drain(markers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
