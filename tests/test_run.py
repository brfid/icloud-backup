"""Run-level tests for cleanup failures, alarm persistence, and locking."""

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


POLICY = ib.Retention(daily=7, weekly=4, monthly=12)
NEW_MOMENT = dt.datetime(2026, 8, 23, 15, 4, 5, tzinfo=dt.UTC)
NEW_STAMP = "20260823T150405Z"


def make_config(root: Path) -> tuple[ib.Config, ib.Source, Path]:
    source_path = root / "source"
    source_path.mkdir()
    (source_path / "important.txt").write_text("keep this\n", encoding="utf-8")
    source = ib.Source(name="source", path=source_path, excludes=())
    destination = root / "backups"
    source_destination = destination / source.name
    source_destination.mkdir(parents=True)
    config = ib.Config(
        dest_root=destination,
        retention=POLICY,
        sources=(source,),
    )
    return config, source, source_destination


class CleanupFailureTests(unittest.TestCase):
    def test_failed_deletion_keeps_extra_archive_and_makes_run_visibly_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, _source, destination = make_config(root)
            old_stamps = [
                NEW_MOMENT - dt.timedelta(days=offset)
                for offset in range(1, 401)
            ]
            old_paths = {
                stamp: destination / f"{ib.utc_stamp(stamp)}.tar.gz"
                for stamp in old_stamps
            }
            for path in old_paths.values():
                path.write_bytes(b"completed archive placeholder")
            keep = ib.gfs_keep([NEW_MOMENT, *old_stamps], POLICY)
            failed_stamp = next(stamp for stamp in old_stamps if stamp not in keep)
            failed_path = old_paths[failed_stamp]
            alarm = root / "Desktop" / "backup-failing.txt"
            alarm.parent.mkdir()
            lock_path = root / "run.lock"
            original_unlink = Path.unlink

            def fail_one_deletion(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> None:
                # Runtime safety checks resolve /var to /private/var on macOS.
                if path.name == failed_path.name:
                    raise OSError("simulated deletion failure")
                original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(ib, "utc_stamp", return_value=NEW_STAMP),
                mock.patch.object(ib, "DESKTOP_FLAG", alarm),
                mock.patch.object(ib, "RUN_LOCK", lock_path),
                mock.patch.object(ib, "notify") as notify,
                mock.patch.object(ib, "log") as log,
                mock.patch.object(
                    Path,
                    "unlink",
                    autospec=True,
                    side_effect=fail_one_deletion,
                ),
            ):
                result = ib.cmd_run(config)

            self.assertEqual(result, 1)
            self.assertTrue((destination / f"{NEW_STAMP}.tar.gz").is_file())
            self.assertTrue(failed_path.is_file())
            self.assertIn(
                "simulated deletion failure",
                alarm.read_text(encoding="utf-8"),
            )
            notify.assert_called()
            self.assertIn(
                "simulated deletion failure",
                "\n".join(str(call) for call in log.call_args_list),
            )


class FullSnapshotTests(unittest.TestCase):
    def test_unchanged_source_is_archived_on_each_daily_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, source, destination = make_config(root)
            first = "20260822T150405Z"
            second = "20260823T150405Z"

            with (
                mock.patch.object(
                    ib, "utc_stamp", side_effect=[first, first, second, second]
                ),
                mock.patch.object(ib, "log"),
            ):
                ib.backup_source(config, source)
                ib.backup_source(config, source)

            self.assertEqual(
                {path.name for path in destination.glob("*.tar.gz")},
                {f"{first}.tar.gz", f"{second}.tar.gz"},
            )


class AlarmClearingTests(unittest.TestCase):
    def test_failure_to_clear_alarm_makes_clean_run_fail_visibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, _source, _destination = make_config(root)
            with (
                mock.patch.object(ib, "RUN_LOCK", root / "run.lock"),
                mock.patch.object(ib, "backup_source"),
                mock.patch.object(
                    ib,
                    "clear_desktop_flag",
                    side_effect=RuntimeError("simulated alarm clear failure"),
                ),
                mock.patch.object(ib, "notify") as notify,
                mock.patch.object(ib, "raise_desktop_flag") as raise_flag,
                mock.patch.object(ib, "log") as log,
            ):
                result = ib.cmd_run(config)

            self.assertEqual(result, 1)
            notify.assert_called_once()
            raise_flag.assert_called_once()
            self.assertIn("alarm clear failure", str(log.call_args))

    def test_alarm_write_failure_is_reported_to_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_parent = Path(tmp) / "missing" / "backup-failing.txt"
            stderr = io.StringIO()

            with (
                mock.patch.object(ib, "DESKTOP_FLAG", missing_parent),
                contextlib.redirect_stderr(stderr),
            ):
                result = ib.raise_desktop_flag("backup failed\n")

            self.assertFalse(result)
            self.assertIn("could not write", stderr.getvalue())
            self.assertIn(str(missing_parent), stderr.getvalue())


class MultiSourceFailureTests(unittest.TestCase):
    def test_one_failure_does_not_prevent_later_sources_from_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_path = root / "first"
            second_path = root / "second"
            first_path.mkdir()
            second_path.mkdir()
            first = ib.Source("first", first_path)
            second = ib.Source("second", second_path)
            config = ib.Config(root / "backups", POLICY, (first, second))
            alarm = root / "alarm.txt"

            with (
                mock.patch.object(ib, "RUN_LOCK", root / "run.lock"),
                mock.patch.object(ib, "DESKTOP_FLAG", alarm),
                mock.patch.object(
                    ib,
                    "backup_source",
                    side_effect=[RuntimeError("first failed"), mock.sentinel.success],
                ) as backup,
                mock.patch.object(ib, "notify"),
                mock.patch.object(ib, "log"),
            ):
                result = ib.cmd_run(config)

            self.assertEqual(result, 1)
            self.assertEqual(
                [call.args[1] for call in backup.call_args_list],
                [first, second],
            )
            self.assertIn("first failed", alarm.read_text(encoding="utf-8"))


class RunLockTests(unittest.TestCase):
    def test_second_run_cannot_take_lock_while_first_holds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "run.lock"
            with ib.run_lock(lock_path):
                with self.assertRaises(RuntimeError):
                    with ib.run_lock(lock_path):
                        self.fail("second lock unexpectedly acquired")


if __name__ == "__main__":
    unittest.main()
