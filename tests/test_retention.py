"""Check the actual Restic policy against independent calendar expectations."""
import datetime as dt
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_backup import Fixture, RESTIC
import icloud_backup as ib


@unittest.skipUnless(RESTIC, 'Restic executable required')
class CalendarRetention(Fixture):
    def test_14_daily_8_weekly_24_monthly_across_year_and_leap_boundaries(self):
        self.app.initialize()
        latest = dt.datetime(2026, 1, 10, 12, tzinfo=dt.UTC)
        dates = {latest - dt.timedelta(days=d) for d in range(20)}
        dates |= {latest - dt.timedelta(weeks=w) for w in range(12)}
        for offset in range(28):
            year, month = divmod(2026 * 12 - offset, 12)
            dates.add(dt.datetime(year, month + 1, 1, 12, tzinfo=dt.UTC))
        dates |= {dt.datetime(2024, 2, 29, 23, 59, tzinfo=dt.UTC),
                  dt.datetime(2025, 12, 31, 23, 59, tzinfo=dt.UTC),
                  dt.datetime(2026, 1, 1, 0, 1, tzinfo=dt.UTC)}
        for i, date in enumerate(sorted(dates)):
            self.app.engine('backup', '--quiet', '--time', date.strftime('%Y-%m-%d %H:%M:%S'),
                            '--tag', f'{ib.MANAGED},complete,source:source,run:{i}', str(self.source))
        before = self.app.snapshots('complete')
        records = sorted(before, key=lambda s: ib.parse_time(s['time']), reverse=True)
        expected = {records[0]['id']}
        for count, calendar_key in [(14, lambda d: d.date()),
                                    (8, lambda d: d.isocalendar()[:2]),
                                    (24, lambda d: (d.year, d.month))]:
            buckets = {}
            for item in records:
                buckets.setdefault(calendar_key(ib.parse_time(item['time'])), item['id'])
            self.assertGreater(len(buckets), count, 'fixture must cross each retention boundary')
            expected.update(list(buckets.values())[:count])

        # Other applications and incomplete runs are outside the complete policy.
        self.app.engine('backup', '--quiet', '--tag', 'another-application', str(self.source))
        self.app.engine('backup', '--quiet', '--tag', f'{ib.MANAGED},pending', str(self.source))
        all_before = json.loads(self.app.engine('snapshots', '--json'))
        protected = {s['id'] for s in all_before} - {s['id'] for s in before}
        # A separate, old source retains its own recovery point despite the newer source.
        separate = self.root / 'separate'; separate.mkdir(); (separate / 'file').write_text('separate')
        self.app.engine('backup', '--quiet', '--time', '2020-01-01 12:00:00',
                        '--tag', f'{ib.MANAGED},complete,source:separate,run:old', str(separate))
        separate_ids = {s['id'] for s in self.app.snapshots('complete', 'source:separate')}
        self.app.retain()
        after = json.loads(self.app.engine('snapshots', '--json'))
        self.assertEqual({s['id'] for s in after}, expected | protected | separate_ids)
        self.assertLess(len(expected), len(before))
        self.app.engine('prune', '--max-repack-size', '0')
        self.app.engine('check', '--read-data')


if __name__ == '__main__':
    unittest.main()
