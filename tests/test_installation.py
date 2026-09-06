"""Verify scheduler wiring and recovery from a missing development checkout."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_backup import Fixture, RESTIC
import icloud_backup as ib
import install
import watch


class Installation(Fixture):
    def test_jobs_use_stable_runtime_and_have_catchup_and_separate_watchdog(self):
        home = self.root/'home with spaces'
        config = home/'.config/icloud_backup/config.toml'
        runner, watcher, launcher, plists = install.plans(home, config, sys.executable, 'local.example')
        backup, checker = plists.values()
        self.assertEqual(backup['StartCalendarInterval'], {'Hour':3, 'Minute':0})
        self.assertEqual(backup['StartInterval'], 3600)
        self.assertTrue(backup['RunAtLoad'])
        self.assertTrue(checker['RunAtLoad'])
        self.assertEqual(checker['StartInterval'], 21600)
        self.assertEqual(backup['ProgramArguments'][1], str(runner))
        self.assertEqual(checker['ProgramArguments'][1], str(watcher))
        self.assertNotEqual(runner, watcher)
        self.assertIn('Application Support', str(runner))
        self.assertEqual(shlex.split(launcher.splitlines()[1])[1:3], [sys.executable, str(runner)])
        self.assertEqual(shlex.split(launcher.splitlines()[1])[3:5], ['--config', str(config)])

    def test_watchdog_reports_a_missing_or_broken_runner_without_importing_it(self):
        config = self.root/'watch-config.toml'
        config.write_text('[runtime]\nflag_path='+json.dumps(str(self.c.flag_path))+'\n')
        with mock.patch.object(sys, 'platform', 'test'):
            result = watch.main(['--config', str(config), '--runner', str(self.root/'missing-runner.py')])
        self.assertEqual(result, 1)
        self.assertIn('could not run', self.c.flag_path.read_text())

    @unittest.skipUnless(RESTIC, 'Restic executable required')
    def test_copied_runtime_backs_up_after_checkout_is_deleted(self):
        development = self.root/'development'; development.mkdir()
        checkout = development/'icloud_backup.py'; shutil.copy2(ib.__file__, checkout)
        runtime = self.root/'runtime'; runtime.mkdir()
        program = runtime/checkout.name; shutil.copy2(checkout, program)
        shutil.rmtree(development)
        config = self.root/'config.toml'
        config.write_text(f'dest_root={json.dumps(str(self.c.dest_root))}\n[runtime]\nstate_dir={json.dumps(str(self.c.state_dir))}\ncache_dir={json.dumps(str(self.c.cache_dir))}\nflag_path={json.dumps(str(self.c.flag_path))}\nrestic={json.dumps(RESTIC)}\nreserve_mib=0\n[[source]]\nname="source"\npath={json.dumps(str(self.source))}\n')
        self.app.initialize()
        result = subprocess.run([sys.executable, str(program), '--config', str(config), 'run'], capture_output=True, text=True, cwd='/')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIsNone(ib.App(self.c).state['last_error'])
        self.assertTrue(ib.App(self.c).state['sources']['source']['snapshot'])


if __name__ == '__main__':
    unittest.main()
