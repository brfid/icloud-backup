"""Filename-only status and independent watchdog health tests."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import icloud_backup as ib  # noqa: E402
import watch  # noqa: E402


POLICY = ib.Retention(daily=7, weekly=4, monthly=12)
NOW = dt.datetime(2026, 8, 23, 18, tzinfo=dt.UTC)


def source(name: str, root: Path) -> ib.Source:
    path = root / f"input-{name}"
    path.mkdir()
    return ib.Source(name=name, path=path, excludes=())


def completed(destination: Path, moment: dt.datetime) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{ib.utc_stamp(moment)}.tar.gz"
    path.write_bytes(b"status must not open this placeholder")
    return path


class StatusTests(unittest.TestCase):
    def test_status_reports_age_count_excess_and_multiple_retention_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = source("documents", root)
            config = ib.Config(
                dest_root=root / "backups",
                retention=POLICY,
                sources=(item,),
            )
            destination = config.dest_for(item.name)
            stamps = [NOW - dt.timedelta(hours=3, days=offset) for offset in range(30)]
            for stamp in stamps:
                completed(destination, stamp)
            (destination / "not-a-timestamp.tar.gz").write_bytes(b"ignored")
            (destination / ".20260823T150000Z.unique.partial").write_bytes(b"ignored")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = ib.cmd_status(config, now=NOW)
            rendered = output.getvalue()

            self.assertEqual(result, 0)
            self.assertIn("documents", rendered)
            self.assertIn("30 archives", rendered)
            self.assertIn("age: 3.0h", rendered)
            self.assertIn("excess", rendered.lower())
            self.assertIn("unrecognized archive name", rendered.lower())
            newest_line = next(
                line
                for line in rendered.splitlines()
                if ib.utc_stamp(max(stamps)) in line and "newest:" not in line
            )
            self.assertIn("daily", newest_line)
            self.assertIn("weekly", newest_line)
            self.assertIn("monthly", newest_line)


class WatchdogTests(unittest.TestCase):
    def test_health_distinguishes_fresh_stale_and_never_backed_up_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh = source("fresh", root)
            stale = source("stale", root)
            never = source("never", root)
            config = ib.Config(
                dest_root=root / "backups",
                retention=POLICY,
                sources=(fresh, stale, never),
            )
            completed(config.dest_for(fresh.name), NOW - dt.timedelta(hours=35))
            completed(config.dest_for(stale.name), NOW - dt.timedelta(hours=37))
            never_destination = config.dest_for(never.name)
            never_destination.mkdir(parents=True)
            (never_destination / "unreadable-name.tar.gz").write_bytes(b"ignored")

            problems = watch.find_problems(config, now=NOW)

            self.assertEqual(len(problems), 2)
            rendered = "\n".join(problems)
            self.assertNotIn("fresh", rendered)
            self.assertIn("stale", rendered)
            self.assertIn("never", rendered)
            self.assertNotIn("unrecognized archive name", rendered)

    def test_future_archive_timestamp_is_not_reported_as_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = source("future", root)
            config = ib.Config(root / "backups", POLICY, (item,))
            completed(config.dest_for(item.name), NOW + dt.timedelta(hours=2))

            problems = watch.find_problems(config, now=NOW)

            self.assertEqual(len(problems), 1)
            self.assertIn("future", problems[0])

    def test_watchdog_raises_alarm_for_problems_and_never_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = source("documents", root)
            config = ib.Config(
                dest_root=root / "backups",
                retention=POLICY,
                sources=(item,),
            )
            with (
                mock.patch.object(
                    watch.ib,
                    "resolve_config_path",
                    return_value=root / "x",
                ),
                mock.patch.object(watch.ib, "load_config", return_value=config),
                mock.patch.object(watch.ib, "notify") as notify,
                mock.patch.object(watch.ib, "raise_desktop_flag") as raise_flag,
                mock.patch.object(watch.ib, "clear_desktop_flag") as clear_flag,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with mock.patch.object(watch, "find_problems", return_value=[]):
                    self.assertEqual(watch.main([]), 0)
                clear_flag.assert_not_called()

                with mock.patch.object(
                    watch,
                    "find_problems",
                    return_value=["documents: never backed up"],
                ):
                    self.assertEqual(watch.main([]), 1)
                notify.assert_called()
                raise_flag.assert_called()
                clear_flag.assert_not_called()


if __name__ == "__main__":
    unittest.main()
