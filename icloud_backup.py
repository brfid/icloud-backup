#!/usr/bin/env python3
"""Back up configured macOS folders through password-free Restic and local checks."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unicodedata
import uuid

VERSION = '2.0.0'
MANAGED = 'icloud-backup-v2'
DEFAULT_CONFIG = Path.home() / '.config/icloud_backup/config.toml'
DEFAULT_SUPPORT = Path.home() / 'Library/Application Support/icloud-backup'
STALE_HOURS = 36


def now():
    return dt.datetime.now(dt.UTC)


def stamp():
    return now().isoformat(timespec='seconds')


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.UTC)


def log(message):
    print(f'{stamp()} {message}', flush=True)


def atomic_write(path, text, mode=0o600):
    """Publish text with a synced temporary file and atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def unknown(values, allowed, label):
    extra = set(values) - set(allowed)
    if extra:
        raise ValueError(f'unknown {label} setting(s): {", ".join(sorted(extra))}')


def integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{label} must be an integer >= {minimum}')
    return value


def absolute(value):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('paths must be nonempty strings')
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise ValueError(f'path must be absolute or start with ~: {value}')
    return p.resolve()


def overlaps(a, b):
    def key(p):
        return tuple(unicodedata.normalize('NFC', x).casefold() for x in p.resolve().parts)
    a, b = key(a), key(b)
    return a == b[:len(a)] or b == a[:len(b)]


@dataclass(frozen=True)
class Source:
    """A named working folder with component exclusions and an empty-source policy."""
    name: str
    path: Path
    excludes: tuple[str, ...] = ()
    allow_empty: bool = False


@dataclass(frozen=True)
class Config:
    """Operating policy from private TOML, independent of the source checkout."""
    dest_root: Path
    sources: tuple[Source, ...]
    daily: int = 14
    weekly: int = 8
    monthly: int = 24
    state_dir: Path = DEFAULT_SUPPORT / 'state'
    cache_dir: Path = Path.home() / 'Library/Caches/icloud-backup/restic'
    flag_path: Path = Path.home() / 'Desktop/⚠️ BACKUP FAILING.txt'
    restic: str = '/opt/homebrew/bin/restic'
    reserve_mib: int = 1024
    timeout_hours: int = 6

    @property
    def repository(self):
        return self.dest_root / 'restic-v2'

    @property
    def signature(self):
        """Fingerprint source coverage for due checks, excluding other runtime policy."""
        content = [(s.name, str(s.path), s.excludes, s.allow_empty) for s in self.sources]
        return hashlib.sha256(json.dumps(content).encode()).hexdigest()

    def validate_paths(self):
        """Reject aliases and recursive/overlapping roots using resolved macOS paths."""
        private = [self.dest_root, self.state_dir, self.cache_dir]
        if any(overlaps(a, b) for i, a in enumerate(private) for b in private[i+1:]):
            raise ValueError('destination, state, and cache directories must not overlap')
        names = set()
        for i, s in enumerate(self.sources):
            name = s.name.casefold()
            if name in names or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', s.name):
                raise ValueError(f'invalid or duplicate source name: {s.name}')
            names.add(name)
            if any(overlaps(s.path, p) for p in private):
                raise ValueError(f'source {s.name} overlaps backup destination, state, or cache')
            if any(overlaps(s.path, other.path) for other in self.sources[i+1:]):
                raise ValueError(f'source {s.name} overlaps another source')


def load_config(path):
    """Parse one explicitly selected TOML file and reject unsafe or unknown settings.

    The example configuration and current checkout are never implicit fallbacks.
    No files are created or migrated here.
    """
    with path.open('rb') as f:
        raw = tomllib.load(f)
    unknown(raw, ['dest_root', 'retention', 'source', 'runtime'], 'configuration')
    retention = raw.get('retention', {})
    unknown(retention, ['daily', 'weekly', 'monthly'], 'retention')
    runtime = raw.get('runtime', {})
    unknown(runtime, ['state_dir', 'cache_dir', 'flag_path', 'restic', 'reserve_mib', 'timeout_hours'], 'runtime')
    entries = raw.get('source', [])
    if not entries:
        raise ValueError('configure at least one [[source]]')
    sources = []
    for entry in entries:
        unknown(entry, ['name', 'path', 'excludes', 'allow_empty'], 'source')
        patterns = entry.get('excludes', [])
        if not isinstance(patterns, list) or not all(isinstance(p, str) and p and '/' not in p and '\0' not in p for p in patterns):
            raise ValueError('excludes must be nonempty component-name globs without slashes')
        allow_empty = entry.get('allow_empty', False)
        if type(allow_empty) is not bool:
            raise ValueError('allow_empty must be true or false')
        sources.append(Source(entry['name'], absolute(entry['path']), tuple(patterns), allow_empty))
    options = {k: absolute(runtime[k]) for k in ['state_dir', 'cache_dir', 'flag_path'] if k in runtime}
    if 'restic' in runtime:
        options['restic'] = str(absolute(runtime['restic']))
    options['reserve_mib'] = integer(runtime.get('reserve_mib', 1024), 'reserve_mib', 0)
    options['timeout_hours'] = integer(runtime.get('timeout_hours', 6), 'timeout_hours')
    c = Config(absolute(raw['dest_root']), tuple(sources),
               **{k: integer(retention.get(k, v), k) for k, v in [('daily', 14), ('weekly', 8), ('monthly', 24)]}, **options)
    c.validate_paths()
    return c


class Busy(RuntimeError):
    pass


@contextmanager
def run_lock(path):
    """Hold a nonblocking local operation lock; raise Busy on contention."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy('another backup, check, or restore is running') from exc
        yield


def stop_child(p):
    """Terminate the owned engine process group and reap its leader."""
    if p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()


def preflight_source(source):
    """Reject missing/empty sources and legacy placeholders without reading payloads."""
    if not source.path.is_dir():
        raise RuntimeError(f'{source.name}: source directory is missing')
    def excluded(name):
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in source.excludes)
    def walk_error(error):
        raise error
    any_entry = False
    for base, directories, files in os.walk(source.path, followlinks=False, onerror=walk_error):
        directories[:] = [name for name in directories if not excluded(name)]
        included = directories + [name for name in files if not excluded(name)]
        any_entry |= bool(included)
        if any(name.startswith('.') and name.endswith('.icloud') for name in included):
            raise RuntimeError(f'{source.name}: legacy .icloud placeholder found; download the source in Finder before retrying')
    if not any_entry and not source.allow_empty:
        raise RuntimeError(f'{source.name}: source is empty after exclusions; set allow_empty=true if intentional')


def notify(message):
    if sys.platform != 'darwin':
        return
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "iCloud Backup"\nend run'
    try:
        subprocess.run(['/usr/bin/osascript', '-e', script, message[:500]], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


class App:
    """Own local orchestration and status; delegate backup storage to Restic."""
    def __init__(self, config):
        self.c = config
        self.state_file = config.state_dir / 'state.json'
        try:
            self.state = json.loads(self.state_file.read_text())
        except FileNotFoundError:
            self.state = {}
        if not isinstance(self.state, dict):
            raise RuntimeError('invalid backup state')

    def save(self):
        atomic_write(self.state_file, json.dumps(self.state, indent=2) + '\n')

    def alert(self, message):
        text = f'iCloud backup needs attention — {stamp()}\n\n{message}\n\nRun: icloud-backup status\n'
        try:
            atomic_write(self.c.flag_path, text)
        except OSError as exc:
            log(f'could not write Desktop warning: {exc}')
        notify(message)

    def space_check(self, extra=()):
        for path in [self.c.dest_root, self.c.state_dir, *extra]:
            while not path.exists():
                path = path.parent
            if shutil.disk_usage(path).free < self.c.reserve_mib * 1024**2:
                raise RuntimeError(f'less than {self.c.reserve_mib} MiB immediately free; stopped to preserve space')

    def engine(self, *args, extra_space=()):
        """Run Restic with explicit repository, empty password, and local guards.

        Capture private diagnostics, scrub inherited RESTIC_* settings, and stop
        the child process group on interruption, timeout, or low free space.
        Callers serialize repository operations with run_lock.
        """
        self.c.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs = self.c.state_dir / 'commands'
        logs.mkdir(exist_ok=True, mode=0o700)
        logfile = logs / f'{time.time_ns()}-{args[0]}.log'
        # Never inherit another repository or credentials from the interactive shell.
        env = {k: v for k, v in os.environ.items() if not k.startswith('RESTIC_')}
        env['TZ'] = 'UTC'
        cmd = [self.c.restic, '--repo', str(self.c.repository), '--insecure-no-password',
               '--cache-dir', str(self.c.cache_dir), *map(str, args)]
        self.space_check(extra_space)
        started = time.monotonic()
        with logfile.open('wb') as f:
            p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            try:
                while p.poll() is None:
                    if time.monotonic() - started > self.c.timeout_hours * 3600:
                        raise RuntimeError('backup operation exceeded its time limit')
                    self.space_check(extra_space)
                    time.sleep(.2)
            except BaseException:
                stop_child(p)
                raise
        output = logfile.read_text(errors='replace')
        for old in sorted(logs.glob('*.log'))[:-40]:
            old.unlink(missing_ok=True)
        if p.returncode:
            raise RuntimeError(f'Restic {args[0]} failed (exit {p.returncode}):\n{output[-3000:]}')
        return output

    def snapshots(self, *tags):
        tag = ','.join([MANAGED, *tags])
        return json.loads(self.engine('snapshots', '--json', '--tag', tag)) or []

    def identity(self):
        if not (self.c.repository / 'config').is_file():
            raise RuntimeError('repository is missing; run never creates a replacement automatically')
        data = json.loads(self.engine('cat', 'config'))
        expected = self.state.get('repository_id')
        if not expected or data['id'] != expected:
            raise RuntimeError('repository identity is not registered or does not match; use init or connect explicitly')

    def initialize(self, connect=False):
        """Explicitly create or verify/register a store; never replace its identity.

        Connecting checks all stored data but does not reconstruct source status.
        Normal backup runs call identity(), not this provisioning operation.
        """
        with run_lock(self.c.state_dir / 'run.lock'):
            if connect:
                if not (self.c.repository / 'config').is_file():
                    raise RuntimeError('no repository to connect to')
                self.engine('check', '--read-data')
            else:
                if self.state.get('repository_id'):
                    raise RuntimeError('already initialized; refusing to replace repository identity')
                if self.c.repository.exists() and any(self.c.repository.iterdir()):
                    raise RuntimeError('repository directory is not empty; use connect for an existing repository')
                if not self.c.dest_root.parent.is_dir():
                    raise RuntimeError('destination parent is missing; refusing to create a substitute iCloud directory')
                self.c.dest_root.mkdir(exist_ok=True, mode=0o700)
                self.engine('init')
            data = json.loads(self.engine('cat', 'config'))
            if self.state.get('repository_id') not in (None, data['id']):
                raise RuntimeError('existing local identity does not match this repository')
            self.state['repository_id'] = data['id']
            self.state['repository'] = str(self.c.repository)
            self.save()
            log('repository ready; no password is required')

    def retain(self):
        """Apply Restic policy to managed complete snapshots, grouped by source path."""
        # Group by paths, not tags: run tags change every day and must not create groups.
        self.engine('forget', '--tag', f'{MANAGED},complete', '--group-by', 'paths',
                    '--keep-last', '1', '--keep-daily', str(self.c.daily),
                    '--keep-weekly', str(self.c.weekly), '--keep-monthly', str(self.c.monthly))

    def run(self):
        """Capture and check sources under one lock before allowing retention.

        Successful peers can gain completed recovery points when another source
        fails. Any source/check failure skips retention and records an alert.
        Pending snapshots never count as completed recovery points.
        """
        with run_lock(self.c.state_dir / 'run.lock'):
            started = now()
            run_tag = 'run:' + uuid.uuid4().hex
            self.state.update(running={'pid':os.getpid(), 'started':started.isoformat()}, last_attempt=stamp())
            self.save()
            try:
                self.c.validate_paths()
                self.identity()
                self.engine('unlock')  # Only Restic's stale locks; never --remove-all.
                successful = set()
                failures = []
                for source in self.c.sources:
                    log(f'backing up {source.name}')
                    args = ['backup', '--json', '--host', MANAGED, '--tag', MANAGED,
                            '--tag', f'source:{source.name}', '--tag', run_tag, '--tag', 'pending']
                    for pattern in source.excludes:
                        args += ['--exclude', pattern]
                    try:
                        preflight_source(source)
                        self.engine(*args, str(source.path))
                        successful.add(source.name)
                    except Exception as exc:
                        failures.append(f'{source.name}: {exc}')
                        log(f'FAILED {source.name}: {exc}')
                if not successful:
                    raise RuntimeError('\n'.join(failures))
                full = self.state.get('needs_full_check') or not self.state.get('last_full_check') or started - parse_time(self.state['last_full_check']) >= dt.timedelta(days=30)
                log('checking stored backup data' if full else 'checking snapshots and one seventh of stored data')
                check_args = ['check', '--read-data'] if full else ['check', '--read-data-subset', f'{started.toordinal() % 7 + 1}/7']
                self.engine(*check_args)
                created = [s for s in self.snapshots(run_tag, 'pending')
                           if any(f'source:{name}' in s.get('tags', []) for name in successful)]
                if len(created) != len(successful):
                    raise RuntimeError('the run did not create one snapshot for every successful source')
                self.engine('tag', '--remove', 'pending', '--add', 'complete', *[s['id'] for s in created])
                complete = self.snapshots(run_tag, 'complete')
                if len(complete) != len(successful):
                    raise RuntimeError('could not confirm all completed snapshots')
                self.state.setdefault('sources', {})
                for source in self.c.sources:
                    if source.name not in successful:
                        continue
                    item = next(s for s in complete if f'source:{source.name}' in s.get('tags', []))
                    self.state['sources'][source.name] = {'snapshot':item['id'], 'time':item['time'], 'path':str(source.path)}
                if failures:
                    raise RuntimeError('Some sources failed; retention was skipped.\n' + '\n'.join(failures))
                # Failed/incomplete runs are never counted as recovery points. Remove
                # their snapshots only after a checked replacement for the same source.
                source_tags = {f'source:{s.name}' for s in self.c.sources}
                pending = [s['id'] for s in self.snapshots('pending')
                           if source_tags.intersection(s.get('tags', [])) and parse_time(s['time']) <= started]
                if pending:
                    self.engine('forget', *pending)
                all_complete = self.snapshots('complete')
                if any(parse_time(s['time']) > now() + dt.timedelta(minutes=5) for s in all_complete):
                    raise RuntimeError('future-dated snapshot found; retention refused')
                self.retain()
                if full:
                    log('removing unused storage files without repacking shared files')
                    self.engine('prune', '--max-repack-size', '0')
                    self.engine('check')
                    self.state['last_full_check'] = stamp()
                self.state.update(last_success=stamp(), last_error=None,
                                  needs_full_check=False,
                                  config_signature=self.c.signature, last_check=stamp(),
                                  last_check_kind='full' if full else 'structure and 1/7 of packs')
                self.state.pop('running', None)
                self.save()
                self.c.flag_path.unlink(missing_ok=True)
                log('all sources backed up and checked locally; iCloud handles syncing')
            except BaseException as exc:
                self.state.pop('running', None)
                self.state.update(last_error=str(exc) or 'interrupted', last_failure=stamp(), needs_full_check=True)
                self.save()
                self.alert(self.state['last_error'])
                raise

    def due(self, local_now=None):
        """Decide whether local scheduling, a source change, or failure requires work."""
        if self.state.get('last_error') or self.state.get('config_signature') != self.c.signature:
            return True
        last = self.state.get('last_success')
        if not last:
            return True
        local_now = local_now or dt.datetime.now().astimezone()
        scheduled = local_now.replace(hour=3, minute=0, second=0, microsecond=0)
        if local_now < scheduled:
            scheduled -= dt.timedelta(days=1)
        return parse_time(last) < scheduled.astimezone(dt.UTC)

    def problems(self, check_scheduler=True):
        """Read local coverage and deployment health without scanning stored data.

        This is the shared health policy used by status and the watchdog. It
        makes no claim about iCloud upload completion or off-device availability.
        """
        problems = []
        if not (self.c.repository / 'config').is_file():
            problems.append('repository is missing')
        running = self.state.get('running')
        active = False
        if running:
            try:
                os.kill(running['pid'], 0)
                active = now() - parse_time(running['started']) < dt.timedelta(hours=self.c.timeout_hours)
            except ProcessLookupError:
                pass
            if not active:
                problems.append('previous run stopped unexpectedly or exceeded its time limit')
        if self.state.get('last_error'):
            problems.append(self.state['last_error'])
        if self.state.get('config_signature') != self.c.signature and not active:
            problems.append('current source configuration has not completed a backup')
        for source in self.c.sources:
            saved = self.state.get('sources', {}).get(source.name)
            if not source.path.is_dir():
                problems.append(f'{source.name}: source is missing')
            if not saved:
                if not active:
                    problems.append(f'{source.name}: no checked snapshot yet')
            else:
                age = now() - parse_time(saved['time'])
                if age < -dt.timedelta(minutes=5):
                    problems.append(f'{source.name}: snapshot timestamp is in the future')
                elif age > dt.timedelta(hours=STALE_HOURS):
                    problems.append(f'{source.name}: backup is more than {STALE_HOURS} hours old')
        installed = self.state.get('installation')
        if check_scheduler and installed and sys.platform == 'darwin':
            if not Path(installed['program']).is_file():
                problems.append('installed backup program is missing')
            if not Path(installed.get('watchdog', installed['program'])).is_file():
                problems.append('installed backup watchdog is missing')
            for label in installed['labels']:
                r = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True, timeout=10)
                if r.returncode:
                    problems.append(f'scheduled job is not loaded: {label}')
        return problems

    def status(self, as_json=False, watch=False):
        problems = self.problems()
        if as_json:
            print(json.dumps({'version':VERSION, 'repository':str(self.c.repository), 'state':self.state, 'problems':problems}, indent=2))
        else:
            print(f'Repository: {self.c.repository}\nRetention: {self.c.daily} daily / {self.c.weekly} weekly / {self.c.monthly} monthly')
            for source in self.c.sources:
                saved = self.state.get('sources', {}).get(source.name, {})
                print(f'{source.name}: {saved.get("time", "never")}  {saved.get("snapshot", "")[:8]}')
            print('Running' if self.state.get('running') else 'Idle')
            print('Local backup status; iCloud upload is assumed, not monitored.')
            for problem in problems:
                print(f'ATTENTION: {problem}')
            if not problems:
                print('OK')
        if watch and problems:
            self.alert('\n'.join(problems))
        return 1 if problems else 0

    def restore(self, source_name, snapshot, target):
        """Restore a completed point into an isolated empty target and verify it."""
        source = next((s for s in self.c.sources if s.name == source_name), None)
        if source is None:
            raise ValueError('unknown source name')
        target = target.expanduser().resolve()
        for p in [self.c.dest_root, self.c.state_dir, self.c.cache_dir, *(s.path for s in self.c.sources)]:
            if overlaps(target, p):
                raise ValueError('restore target overlaps a source or backup storage')
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError('restore target must be a new or empty directory')
        with run_lock(self.c.state_dir / 'run.lock'):
            self.identity()
            choices = self.snapshots('complete', f'source:{source_name}')
            matches = choices if snapshot == 'latest' else [s for s in choices if s['id'].startswith(snapshot)]
            if not matches or (snapshot != 'latest' and len(matches) != 1):
                raise ValueError('snapshot not found or prefix is ambiguous')
            selected = max(matches, key=lambda s: s['time'])
            # Use the path recorded in that snapshot, even if config paths changed.
            path = selected['paths'][0]
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.engine('restore', f'{selected["id"]}:{path}', '--verify', '--target', str(target), extra_space=[target])
            log(f'restored {source_name} snapshot {selected["id"][:8]} into {target}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('ICLOUD_BACKUP_CONFIG', DEFAULT_CONFIG)))
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ['init', 'connect', 'run', 'tick', 'watch', 'snapshots']:
        sub.add_parser(name)
    status = sub.add_parser('status'); status.add_argument('--json', action='store_true')
    verify = sub.add_parser('verify'); verify.add_argument('--full', action='store_true')
    restore = sub.add_parser('restore')
    restore.add_argument('source'); restore.add_argument('--snapshot', default='latest')
    restore.add_argument('--target', required=True, type=Path)
    args = parser.parse_args(argv)
    app = None
    try:
        app = App(load_config(args.config.expanduser()))
        if args.command in ['init', 'connect']:
            app.initialize(connect=args.command == 'connect')
        elif args.command in ['run', 'tick']:
            if args.command == 'run' or app.due():
                app.run()
            else:
                log('next daily backup is not due yet')
        elif args.command in ['status', 'watch']:
            return app.status(as_json=getattr(args, 'json', False), watch=args.command == 'watch')
        elif args.command == 'restore':
            app.restore(args.source, args.snapshot, args.target)
        else:
            with run_lock(app.c.state_dir / 'run.lock'):
                app.identity()
                if args.command == 'snapshots':
                    items = app.snapshots('complete')
                    for item in sorted(items, key=lambda s: s['time'], reverse=True):
                        names = [t.removeprefix('source:') for t in item.get('tags', []) if t.startswith('source:')]
                        print(f'{item["id"][:8]}  {item["time"]}  {", ".join(names)}')
                else:
                    app.engine('check', *(['--read-data'] if args.full else []))
                    log('integrity check passed')
        return 0
    except Busy as exc:
        log(str(exc))
        return 0 if args.command == 'tick' else 2
    except (Exception, KeyboardInterrupt) as exc:
        message = str(exc) or 'interrupted'
        print(message, file=sys.stderr)
        if app and args.command == 'verify':
            app.state.update(last_error=message, last_failure=stamp(), needs_full_check=True)
            app.save()
            app.alert(message)
        if args.command == 'watch':
            if app:
                app.alert(message)
            else:
                notify('Backup checker failed: ' + message)
                try:
                    atomic_write(Path.home() / 'Desktop/⚠️ BACKUP FAILING.txt', 'Backup checker failed: ' + message + '\n')
                except OSError:
                    pass
        return 1


if __name__ == '__main__':
    os.umask(0o077)
    def terminate(signum, frame):
        raise InterruptedError('interrupted by scheduler or shutdown')
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
