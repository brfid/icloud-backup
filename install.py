#!/usr/bin/env python3
"""Deploy copies of this checkout as a stable macOS runtime and two launchd jobs."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time

import icloud_backup as ib


def plans(home, config, interpreter, label):
    """Build deployment paths, shortcut text, and plists without writing files.

    All entry points select the same config explicitly and run installed copies.
    These fixed deployment paths are separate from Config runtime overrides.
    """
    runtime = home/'Library/Application Support/icloud-backup/runtime'
    runner, watcher = runtime/'icloud_backup.py', runtime/'watch.py'
    agents, logs = home/'Library/LaunchAgents', home/'Library/Logs'
    base = {'RunAtLoad': True, 'ProcessType': 'Background', 'Nice': 10,
            'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'}}
    backup = dict(base, Label=label, ProgramArguments=[interpreter, str(runner), '--config', str(config), 'tick'],
                  StartCalendarInterval={'Hour': 3, 'Minute': 0}, StartInterval=3600,
                  StandardOutPath=str(logs/'icloud-backup.log'), StandardErrorPath=str(logs/'icloud-backup.log'))
    watch = dict(base, Label=label+'-watch', ProgramArguments=[interpreter, str(watcher), '--runner', str(runner), '--config', str(config)],
                 StartInterval=21600, StandardOutPath=str(logs/'icloud-backup-watch.log'), StandardErrorPath=str(logs/'icloud-backup-watch.log'))
    launcher = '#!/bin/sh\nexec ' + ' '.join(shlex.quote(x) for x in [interpreter, str(runner), '--config', str(config)]) + ' "$@"\n'
    return runner, watcher, launcher, {agents/(label+'.plist'): backup, agents/(label+'-watch.plist'): watch}


def stable_python():
    """Prefer a supported Homebrew command that survives routine version upgrades."""
    for path in ['/opt/homebrew/bin/python3', '/usr/local/bin/python3', sys.executable]:
        if Path(path).is_file():
            result = subprocess.run([path, '-c', 'import sys; print(int(sys.version_info >= (3, 11)))'], capture_output=True, text=True, check=True)
            if result.stdout.strip() == '1':
                return path
    raise RuntimeError('Python 3.11 or newer is required')


def install(config_path, label, initialize):
    """Replace a deployment, preserve private rollback files, and load its jobs.

    This unloads the selected jobs and may start a due backup after installation.
    It exports recovery copies but never overwrites the selected active config.
    Existing installations must supply their actual config and base label; label
    discovery and changing unrelated jobs are not responsibilities of this call.
    """
    if sys.platform != 'darwin':
        raise RuntimeError('launchd installation requires macOS')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]+', label):
        raise ValueError('invalid launchd label')
    config_path = config_path.expanduser().resolve()
    config = ib.load_config(config_path)
    version = subprocess.run([config.restic, 'version'], capture_output=True, text=True, check=True).stdout
    match = re.search(r'restic (\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (0, 19, 1):
        raise RuntimeError('Restic 0.19.1 or newer is required')
    interpreter = stable_python()
    home = Path.home()
    runner, watcher, launcher, plists = plans(home, config_path, interpreter, label)
    support = runner.parent.parent
    migration = support/'migration'/dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%S.%fZ')
    migration.mkdir(parents=True, mode=0o700)
    # Keep private rollback material outside the source checkout.
    shutil.copy2(config_path, migration/'config.toml')
    for path in [runner, watcher, home/'bin/icloud-backup', *plists]:
        if path.is_file():
            shutil.copy2(path, migration/path.name)
    domain = f'gui/{os.getuid()}'
    previous = {}
    for job in [label+'-watch', label]:
        loaded = subprocess.run(['/bin/launchctl', 'print', domain+'/'+job], capture_output=True).returncode == 0
        previous[job] = loaded
        if loaded:
            subprocess.run(['/bin/launchctl', 'bootout', domain+'/'+job], check=True, capture_output=True)
    ib.atomic_write(migration/'previous-jobs.json', json.dumps(previous, indent=2)+'\n')
    app = ib.App(config)
    with ib.run_lock(config.state_dir/'run.lock'):
        for filename, target in [('icloud_backup.py', runner), ('watch.py', watcher)]:
            ib.atomic_write(target, Path(__file__).with_name(filename).read_text(), 0o700)
        ib.atomic_write(home/'bin/icloud-backup', launcher, 0o700)
        for path, data in plists.items():
            ib.atomic_write(path, plistlib.dumps(data).decode())
            subprocess.run(['/usr/bin/plutil', '-lint', str(path)], check=True)
        (home/'Library/Logs').mkdir(parents=True, exist_ok=True)
        app.state['installation'] = {'program': str(runner), 'watchdog': str(watcher),
                                     'interpreter': interpreter, 'config': str(config_path),
                                     'labels': [label, label+'-watch'], 'installed_at': ib.stamp(), 'version': ib.VERSION}
        app.save()
    if not app.state.get('repository_id'):
        if not initialize:
            raise RuntimeError('Run init or connect before installing, or pass --initialize for a new repository; jobs were not loaded')
        app.initialize()
    else:
        app.identity()
    recovery = config.dest_root/'restic-v2-recovery'
    ib.atomic_write(recovery/'RECOVERY.txt', Path(__file__).with_name('RECOVERY.txt').read_text())
    ib.atomic_write(recovery/'config-at-install.toml', config_path.read_text())
    # A prior `launchctl disable` survives replacement of the plist and otherwise
    # makes bootstrap fail with an opaque input/output error.
    for job in [label, label+'-watch']:
        subprocess.run(['/bin/launchctl', 'enable', domain+'/'+job], check=True, capture_output=True)
    backup_plist, watch_plist = plists
    subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(backup_plist)], check=True, capture_output=True)
    # Give the backup job a chance to mark its first attempt before the initial check.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        latest = ib.App(config).state
        if latest.get('running') or latest.get('last_success') or latest.get('last_error'):
            break
        time.sleep(.2)
    subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(watch_plist)], check=True, capture_output=True)
    print(f'Installed runtime: {runner.parent}\nCommand: {home}/bin/icloud-backup\nPrevious files: {migration}')
    print('Daily backup and independent six-hour watchdog are loaded. Use icloud-backup status to check the first run.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ib.DEFAULT_CONFIG)
    parser.add_argument('--label', default='local.icloud-backup')
    parser.add_argument('--initialize', action='store_true', help='initialize a new repository if none is registered')
    args = parser.parse_args()
    try:
        install(args.config, args.label, args.initialize)
    except Exception as exc:
        print(f'Installation failed: {exc}', file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.decode(errors='replace') if isinstance(exc.stderr, bytes) else exc.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    os.umask(0o077)
    raise SystemExit(main())
