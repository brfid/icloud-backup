"""Exercise safety boundaries and real password-free Restic recovery."""
import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import icloud_backup as ib

RESTIC = shutil.which('restic')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root/'source'; self.source.mkdir()
        (self.source/'version.txt').write_text('first version\n')
        self.c = ib.Config(self.root/'backups', (ib.Source('source', self.source, ('.git','*.git','.DS_Store')),),
                           state_dir=self.root/'state', cache_dir=self.root/'cache', flag_path=self.root/'warning.txt',
                           restic=RESTIC or '/missing/restic', reserve_mib=0)
        self.app = ib.App(self.c)
        self.alerts = mock.patch.object(ib, 'notify')
        self.alerts.start()
        self.stdout = contextlib.redirect_stdout(io.StringIO()); self.stdout.__enter__()

    def tearDown(self):
        self.stdout.__exit__(None,None,None)
        self.alerts.stop()
        self.tmp.cleanup()


class ConfigSafety(Fixture):
    def test_destination_recursion_aliases_and_overlaps_rejected(self):
        bad = dataclasses.replace(self.c, dest_root=self.source/'backups')
        with self.assertRaisesRegex(ValueError,'overlaps'):
            bad.validate_paths()
        alias=self.root/'alias'; alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'overlaps'):
            dataclasses.replace(self.c,dest_root=alias/'backups').validate_paths()
        with self.assertRaisesRegex(ValueError,'overlaps'):
            dataclasses.replace(self.c,sources=(self.c.sources[0],ib.Source('nested',self.source/'nested'))).validate_paths()
        with self.assertRaisesRegex(ValueError,'duplicate'):
            dataclasses.replace(self.c,sources=(self.c.sources[0],ib.Source('SOURCE',self.root/'other'))).validate_paths()

    def test_legacy_placeholder_and_missing_or_empty_sources_fail(self):
        (self.source/'.missing.pdf.icloud').write_bytes(b'placeholder')
        with self.assertRaisesRegex(RuntimeError,'placeholder'):
            ib.preflight_source(self.c.sources[0])
        with self.assertRaisesRegex(RuntimeError,'missing'):
            ib.preflight_source(ib.Source('missing',self.root/'absent'))
        empty=self.root/'empty'; empty.mkdir()
        with self.assertRaisesRegex(RuntimeError,'empty'):
            ib.preflight_source(ib.Source('empty',empty))
        ib.preflight_source(ib.Source('empty',empty,allow_empty=True))

    def test_restore_rejects_sources_destination_and_nonempty_target(self):
        for target in [self.source, self.source/'sub', self.c.dest_root, self.root]:
            with self.assertRaises(ValueError):
                self.app.restore('source','latest',target)
        target=self.root/'nonempty';target.mkdir();(target/'unrelated').write_text('keep')
        with self.assertRaisesRegex(ValueError,'empty'):
            self.app.restore('source','latest',target)
        self.assertEqual((target/'unrelated').read_text(),'keep')

    def test_process_lock_releases_after_exception(self):
        lock=self.root/'lock'
        with self.assertRaisesRegex(RuntimeError,'interrupted'):
            with ib.run_lock(lock):
                with self.assertRaises(ib.Busy):
                    with ib.run_lock(lock): pass
                raise RuntimeError('interrupted')
        with ib.run_lock(lock): pass

    def test_daily_schedule_catches_up_after_login_without_duplicate(self):
        local=dt.timezone(dt.timedelta(hours=-4))
        self.app.state.update(config_signature=self.c.signature,last_success='2026-09-05T07:10:00+00:00')
        self.assertFalse(self.app.due(dt.datetime(2026,9,5,14,tzinfo=local)))
        self.assertFalse(self.app.due(dt.datetime(2026,9,6,2,tzinfo=local)))
        self.assertTrue(self.app.due(dt.datetime(2026,9,6,3,tzinfo=local)))
        self.assertTrue(self.app.due(dt.datetime(2026,9,10,10,tzinfo=local)))
        self.app.state['last_error']='failure'
        self.assertTrue(self.app.due(dt.datetime(2026,9,5,14,tzinfo=local)))

    def test_status_reports_stale_missing_and_changed_configuration(self):
        self.c.repository.mkdir(parents=True);(self.c.repository/'config').write_text('fixture')
        self.app.state.update(config_signature=self.c.signature,sources={'source':{'time':(ib.now()-dt.timedelta(hours=40)).isoformat()}})
        self.assertTrue(any('36 hours' in p for p in self.app.problems(False)))
        self.app.state['config_signature']='different'
        self.assertTrue(any('configuration' in p for p in self.app.problems(False)))
        (self.c.repository/'config').unlink()
        self.assertIn('repository is missing',self.app.problems(False))


@unittest.skipUnless(RESTIC,'Restic executable required')
class RealEngine(Fixture):
    def init_run(self):
        self.app.initialize();self.app.run()

    def test_password_free_versions_deletion_hidden_links_and_restore(self):
        (self.source/'deleted.txt').write_text('recover me')
        (self.source/'.hidden').write_text('include hidden')
        (self.source/'link').symlink_to('version.txt')
        (self.source/'.git').mkdir();(self.source/'.git'/'secret-history').write_text('exclude git')
        (self.source/'old.git').mkdir();(self.source/'old.git'/'HEAD').write_text('exclude bare git')
        self.init_run()
        first=self.app.state['sources']['source']['snapshot']
        (self.source/'version.txt').write_text('second version\n')
        (self.source/'deleted.txt').unlink()
        # Do not rotate between the two same-day recovery demonstrations.
        with mock.patch.object(self.app,'retain'):
            self.app.run()
        older=self.root/'older';self.app.restore('source',first,older)
        current=self.root/'current';self.app.restore('source','latest',current)
        self.assertEqual((older/'version.txt').read_text(),'first version\n')
        self.assertEqual((older/'deleted.txt').read_text(),'recover me')
        self.assertEqual((current/'version.txt').read_text(),'second version\n')
        self.assertFalse((current/'deleted.txt').exists())
        self.assertEqual((older/'.hidden').read_text(),'include hidden')
        self.assertTrue((older/'link').is_symlink())
        self.assertFalse((older/'.git').exists());self.assertFalse((older/'old.git').exists())
        # A fresh Restic process needs neither the app state nor any secret.
        r=subprocess.run([RESTIC,'-r',str(self.c.repository),'--insecure-no-password','--no-cache','check','--read-data'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def test_missing_source_keeps_history_and_allows_healthy_source(self):
        self.init_run();previous=self.app.snapshots('complete')
        other=self.root/'other';other.mkdir();(other/'ok').write_text('still protect me')
        self.source.rename(self.root/'temporarily-missing')
        c=dataclasses.replace(self.c,sources=(*self.c.sources,ib.Source('other',other)))
        app=ib.App(c)
        with self.assertRaisesRegex(RuntimeError,'Some sources failed'), mock.patch.object(app,'retain') as retain:
            app.run()
        retain.assert_not_called()
        self.assertIn(previous[0]['id'],[s['id'] for s in app.snapshots('complete')])
        self.assertIn('other',app.state['sources'])
        self.assertTrue(c.flag_path.exists())

    def test_partial_read_failure_never_promoted_or_retained(self):
        self.init_run();previous=self.app.snapshots('complete');engine=self.app.engine
        def partial(*args,**kwargs):
            if args[0]=='backup':
                engine(*args,**kwargs)
                raise RuntimeError('Restic backup failed (exit 3): simulated unreadable source')
            return engine(*args,**kwargs)
        with mock.patch.object(self.app,'engine',side_effect=partial), mock.patch.object(self.app,'retain') as retain:
            with self.assertRaisesRegex(RuntimeError,'exit 3'):self.app.run()
        retain.assert_not_called()
        self.assertEqual([s['id'] for s in self.app.snapshots('complete')],[s['id'] for s in previous])
        self.assertTrue(self.app.snapshots('pending'))
        self.app.run()
        self.assertFalse(self.app.snapshots('pending'))
        self.assertFalse(self.c.flag_path.exists())

    def test_failed_verification_and_low_space_preserve_good_snapshots(self):
        self.init_run();old={s['id'] for s in self.app.snapshots('complete')};engine=self.app.engine
        def fail_check(*args,**kwargs):
            if args[0]=='check':raise RuntimeError('simulated integrity failure')
            return engine(*args,**kwargs)
        with mock.patch.object(self.app,'engine',side_effect=fail_check),mock.patch.object(self.app,'retain') as retain:
            with self.assertRaisesRegex(RuntimeError,'integrity failure'):self.app.run()
        retain.assert_not_called()
        self.assertEqual({s['id'] for s in self.app.snapshots('complete')},old)
        with mock.patch.object(self.app,'space_check',side_effect=RuntimeError('simulated full storage')):
            with self.assertRaisesRegex(RuntimeError,'full storage'):self.app.run()
        self.assertEqual({s['id'] for s in self.app.snapshots('complete')},old)

    def test_repository_replacement_is_not_silently_accepted(self):
        self.init_run()
        self.app.state['repository_id']='wrong'
        with self.assertRaisesRegex(RuntimeError,'identity'):self.app.run()
        self.assertTrue(self.c.flag_path.exists())

    def test_interrupted_engine_releases_lock_and_can_resume(self):
        self.init_run();before={s['id'] for s in self.app.snapshots('complete')}
        blocker=self.root/'blocker.py'
        blocker.write_text('#!/usr/bin/env python3\nimport time\nprint("started",flush=True)\ntime.sleep(60)\n')
        blocker.chmod(0o700)
        config=self.root/'config.toml'
        config.write_text(f'dest_root={json.dumps(str(self.c.dest_root))}\n[runtime]\nstate_dir={json.dumps(str(self.c.state_dir))}\ncache_dir={json.dumps(str(self.c.cache_dir))}\nflag_path={json.dumps(str(self.c.flag_path))}\nrestic={json.dumps(str(blocker))}\nreserve_mib=0\n[[source]]\nname="source"\npath={json.dumps(str(self.source))}\n')
        command=[sys.executable,str(Path(ib.__file__)), '--config',str(config),'run']
        p=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if any('started' in f.read_text() for f in (self.c.state_dir/'commands').glob('*-cat.log')):break
            time.sleep(.05)
        p.terminate();p.communicate(timeout=15)
        self.assertNotEqual(p.returncode,0)
        self.assertEqual({s['id'] for s in self.app.snapshots('complete')},before)
        app=ib.App(self.c)
        self.assertIn('interrupted',app.state['last_error'])
        app.run()
        self.assertFalse(app.state['last_error'])


if __name__=='__main__':unittest.main()
