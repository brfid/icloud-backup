"""Focused retention tests, including pruning against real directory entries."""

from __future__ import annotations

import datetime as dt
import random
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import icloud_backup as ib  # noqa: E402


UTC = dt.UTC
POLICY = ib.Retention(daily=7, weekly=4, monthly=12)


def newest_in_buckets(
    stamps: Iterable[dt.datetime],
    count: int,
    bucket: Callable[[dt.datetime], object],
) -> set[dt.datetime]:
    """Independent, intentionally literal oracle for one GFS tier."""
    newest: dict[object, dt.datetime] = {}
    for stamp in stamps:
        key = bucket(stamp)
        newest[key] = max(stamp, newest.get(key, stamp))
    represented = sorted(newest, reverse=True)[:count]
    return {newest[key] for key in represented}


def expected_keep(stamps: Iterable[dt.datetime]) -> set[dt.datetime]:
    stamps = list(stamps)
    if not stamps:
        return set()
    return (
        {max(stamps)}
        | newest_in_buckets(stamps, POLICY.daily, lambda value: value.date())
        | newest_in_buckets(
            stamps,
            POLICY.weekly,
            lambda value: value.isocalendar()[:2],
        )
        | newest_in_buckets(
            stamps,
            POLICY.monthly,
            lambda value: (value.year, value.month),
        )
    )


def daily_stamps(days: int) -> list[dt.datetime]:
    newest = dt.datetime(2026, 8, 23, 18, 0, tzinfo=UTC)
    return [newest - dt.timedelta(days=offset) for offset in range(days)]


class GfsRetentionTests(unittest.TestCase):
    def test_union_uses_newest_in_each_represented_bucket_with_gaps(self):
        stamps = daily_stamps(18)
        stamps.extend(
            [
                dt.datetime(2026, 6, 2, 8, tzinfo=UTC),
                dt.datetime(2026, 4, 12, 8, tzinfo=UTC),
                dt.datetime(2026, 1, 5, 8, tzinfo=UTC),
                dt.datetime(2025, 11, 8, 8, tzinfo=UTC),
                dt.datetime(2025, 8, 20, 8, tzinfo=UTC),
                dt.datetime(2025, 6, 1, 8, tzinfo=UTC),
                dt.datetime(2025, 3, 3, 8, tzinfo=UTC),
                dt.datetime(2024, 12, 9, 8, tzinfo=UTC),
                dt.datetime(2024, 9, 7, 8, tzinfo=UTC),
                dt.datetime(2024, 5, 4, 8, tzinfo=UTC),
                dt.datetime(2024, 1, 1, 8, tzinfo=UTC),
                dt.datetime(2023, 8, 23, 8, tzinfo=UTC),
            ]
        )

        self.assertEqual(ib.gfs_keep(stamps, POLICY), expected_keep(stamps))

    def test_same_bucket_collapses_to_newest_and_result_is_order_independent(self):
        stamps = daily_stamps(500)
        newest = max(stamps)
        older_same_day = newest.replace(hour=2)
        stamps.append(older_same_day)
        shuffled = stamps[:]
        random.Random(731).shuffle(shuffled)

        expected = expected_keep(stamps)
        self.assertIn(newest, expected)
        self.assertNotIn(older_same_day, expected)
        self.assertEqual(ib.gfs_keep(stamps, POLICY), expected)
        self.assertEqual(ib.gfs_keep(shuffled, POLICY), expected)

    def test_newest_is_always_kept_and_union_never_exceeds_23(self):
        stamps = [
            dt.datetime(2026, 8, 23, 12, tzinfo=UTC)
            - dt.timedelta(hours=12 * offset)
            for offset in range(1_600)
        ]

        keep = ib.gfs_keep(stamps, POLICY)

        self.assertIn(max(stamps), keep)
        self.assertLessEqual(len(keep), 7 + 4 + 12)
        self.assertEqual(keep, expected_keep(stamps))


class RealFilePruningTests(unittest.TestCase):
    def test_survivors_match_keep_set_and_nonarchives_are_ignored(self):
        stamps = daily_stamps(500)
        keep = ib.gfs_keep(stamps, POLICY)

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            archive_for = {
                stamp: destination / f"{ib.utc_stamp(stamp)}.tar.gz"
                for stamp in stamps
            }
            for path in archive_for.values():
                path.write_bytes(b"completed archive placeholder")

            ignored = {
                destination / ".20260823T180000Z.unique.partial",
                destination / "20260823T180000Z.partial",
                destination / "20260823T180000Z.tar.zst",
                destination / "not-a-timestamp.tar.gz",
                destination / "README.txt",
            }
            for path in ignored:
                path.write_bytes(b"leave me alone")

            ib.prune(
                destination,
                POLICY,
                now=max(stamps) + dt.timedelta(hours=1),
            )

            self.assertEqual(
                {stamp for stamp, path in archive_for.items() if path.exists()},
                keep,
            )
            self.assertTrue(all(path.exists() for path in ignored))

    def test_future_dated_archive_refuses_all_destructive_cleanup(self):
        now = dt.datetime(2026, 8, 23, 18, tzinfo=UTC)
        stamps = [now - dt.timedelta(days=offset) for offset in range(30)]
        stamps.append(now + dt.timedelta(hours=2))

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            paths = {
                destination / f"{ib.utc_stamp(stamp)}.tar.gz" for stamp in stamps
            }
            for path in paths:
                path.write_bytes(b"completed archive placeholder")

            with self.assertRaisesRegex(
                ib.RetentionSafetyError,
                "future-dated archive",
            ):
                ib.prune(destination, POLICY, now=now)

            self.assertEqual(set(destination.glob("*.tar.gz")), paths)


if __name__ == "__main__":
    unittest.main()
