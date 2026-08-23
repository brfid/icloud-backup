#!/usr/bin/env python3
"""Check snapshot freshness and report failures."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import icloud_backup as ib


def find_problems(
    config: ib.Config, now: dt.datetime | None = None
) -> list[str]:
    """Return the same read-only health problems reported by ``status``."""
    return ib.health_problems(config, now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="read configuration from PATH",
    )
    args = parser.parse_args(argv)

    try:
        config = ib.load_config(ib.resolve_config_path(args.config))
        problems = find_problems(config)
    except Exception as exc:  # noqa: BLE001 - the watchdog must alarm on its own failure
        problems = [f"watchdog could not check backups: {exc}"]
    if problems:
        detail = "\n".join(f"- {p}" for p in problems)
        ib.notify("iCloud backup STALLED", f"{len(problems)} source(s) need attention")
        ib.raise_desktop_flag(
            f"iCloud backup watchdog: {ib.now_iso()}\n\n{detail}\n\n"
            "Check: icloud_backup.py status   "
            "(log: ~/Library/Logs/icloud-backup.log)\n"
        )
        print(detail, file=sys.stderr)
        return 1
    print("ok: all sources fresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
