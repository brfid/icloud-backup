#!/usr/bin/env python3
"""Watchdog for icloud_backup: report when a source stops backing up.

A backup job cannot report its own absence: if its agent is unloaded, the Mac was
off, or a run hung, no error is produced. This runs independently (at login and
periodically) and raises a macOS notification plus a Desktop flag when any source's
last successful run is older than ``stale_hours``, never ran, or errored.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import icloud_backup as ib


def find_problems(config: ib.Config) -> list[str]:
    """One message per source that is stale, errored, or never backed up; empty if all fresh."""
    now = dt.datetime.now().astimezone()
    problems: list[str] = []
    for source in config.sources:
        state = ib.read_state(source.name)
        last_run = state.get("last_run")
        status = state.get("last_status")
        if not last_run:
            problems.append(f"{source.name}: never backed up")
            continue
        try:
            when = dt.datetime.fromisoformat(str(last_run))
        except ValueError:
            problems.append(f"{source.name}: unreadable state file")
            continue
        age_hours = (now - when).total_seconds() / 3600
        if status == "error":
            problems.append(f"{source.name}: last run errored ({state.get('last_error', '?')})")
        elif age_hours > config.stale_hours:
            problems.append(f"{source.name}: no successful backup in {age_hours:.0f}h")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)

    config = ib.load_config(ib.resolve_config_path(args.config))
    problems = find_problems(config)
    if problems:
        detail = "\n".join(f"- {p}" for p in problems)
        ib.notify("iCloud backup STALLED", f"{len(problems)} source(s) need attention")
        ib.raise_desktop_flag(
            f"iCloud backup watchdog: {ib.now_iso()}\n\n{detail}\n\n"
            "Check: icloud_backup.py list   (log: ~/Library/Logs/icloud-backup.log)\n"
        )
        print(detail, file=sys.stderr)
        return 1
    ib.clear_desktop_flag()
    print("ok: all sources fresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
