"""Unit tests for the parts where a bug would lose data: retention and excludes."""

from __future__ import annotations

import datetime as dt
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import icloud_backup as ib  # noqa: E402


def daily_stamps(days: int) -> list[dt.datetime]:
    base = dt.datetime(2026, 8, 13, 12, 0, tzinfo=dt.UTC)
    return [base - dt.timedelta(days=i) for i in range(days)]


class GfsRetentionTests(unittest.TestCase):
    def test_keeps_all_when_under_quota(self):
        stamps = daily_stamps(5)
        keep = ib.gfs_keep(stamps, ib.Retention(7, 4, 12))
        self.assertEqual(keep, set(stamps))

    def test_always_keeps_newest(self):
        stamps = daily_stamps(400)
        keep = ib.gfs_keep(stamps, ib.Retention(7, 4, 12))
        self.assertIn(max(stamps), keep)

    def test_keeps_seven_most_recent_days(self):
        stamps = daily_stamps(400)
        keep = ib.gfs_keep(stamps, ib.Retention(7, 4, 12))
        for recent in sorted(stamps, reverse=True)[:7]:
            self.assertIn(recent, keep)

    def test_drops_ancient_and_bounds_total(self):
        stamps = daily_stamps(400)
        keep = ib.gfs_keep(stamps, ib.Retention(7, 4, 12))
        self.assertNotIn(min(stamps), keep)          # ~400 days old: gone
        self.assertLessEqual(len(keep), 7 + 4 + 12)  # never more than the quota sum
        self.assertGreaterEqual(len(keep), 12)       # a year of monthlies survives

    def test_year_of_months_represented(self):
        stamps = daily_stamps(400)
        keep = ib.gfs_keep(stamps, ib.Retention(7, 4, 12))
        months = {t.strftime("%Y%m") for t in keep}
        self.assertGreaterEqual(len(months), 12)

    def test_order_independent(self):
        stamps = daily_stamps(200)
        shuffled = stamps[:]
        random.Random(1).shuffle(shuffled)
        self.assertEqual(
            ib.gfs_keep(stamps, ib.Retention(7, 4, 12)),
            ib.gfs_keep(shuffled, ib.Retention(7, 4, 12)),
        )

    def test_one_kept_per_day_when_multiple_same_day(self):
        base = dt.datetime(2026, 8, 13, tzinfo=dt.UTC)
        # three snapshots today, one yesterday
        stamps = [
            base.replace(hour=1),
            base.replace(hour=12),
            base.replace(hour=23),
            base - dt.timedelta(days=1),
        ]
        keep = ib.gfs_keep(stamps, ib.Retention(7, 0, 0))
        self.assertIn(base.replace(hour=23), keep)      # newest of the day
        self.assertNotIn(base.replace(hour=1), keep)    # older same-day pruned


class ExcludeAndFingerprintTests(unittest.TestCase):
    def test_excluded_matches_globs(self):
        pats = [".DS_Store", "*.pyc", "node_modules"]
        self.assertTrue(ib._excluded(".DS_Store", pats))
        self.assertTrue(ib._excluded("a.pyc", pats))
        self.assertTrue(ib._excluded("node_modules", pats))
        self.assertFalse(ib._excluded("keep.py", pats))

    def test_excluded_dirs_pruned_and_fingerprint_reacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "keep").mkdir()
            (root / "keep" / "a.txt").write_text("hello")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "junk.txt").write_text("x" * 100)

            names = {rel for _p, rel, _d in ib.iter_entries(root, ["node_modules"])}
            self.assertIn("keep", names)
            self.assertIn(str(Path("keep") / "a.txt"), names)
            self.assertNotIn("node_modules", names)

            fp1 = ib.fingerprint(root, ["node_modules"])
            # touching an excluded file must NOT change the fingerprint
            os.utime(root / "node_modules" / "junk.txt", (0, 0))
            self.assertEqual(fp1, ib.fingerprint(root, ["node_modules"]))
            # changing an included file MUST change it
            (root / "keep" / "a.txt").write_text("hello world")
            self.assertNotEqual(fp1, ib.fingerprint(root, ["node_modules"]))


if __name__ == "__main__":
    unittest.main()
