#!/usr/bin/env python3
"""Supervise the runner through JSON status, including when it cannot start.

Keep this module independent of runner imports so its fallback alert still works
when that file is missing or broken. Operational health rules remain in the runner.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib


def main(argv=None):
    """Read runner status with a deadline; independently surface failures as alerts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runner', type=Path, default=Path(__file__).with_name('icloud_backup.py'))
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/icloud_backup/config.toml')
    args = parser.parse_args(argv)
    flag = Path.home()/'Desktop/⚠️ BACKUP FAILING.txt'
    try:
        config = tomllib.loads(args.config.expanduser().read_text())
        flag = Path(config.get('runtime', {}).get('flag_path', str(flag))).expanduser()
        result = subprocess.run([sys.executable, str(args.runner), '--config', str(args.config), 'status', '--json'],
                                capture_output=True, text=True, timeout=60)
        try:
            data = json.loads(result.stdout)
            problems = data['problems']
        except (ValueError, KeyError, TypeError):
            raise RuntimeError('Backup status could not run: ' + (result.stderr or result.stdout)[-2000:])
        if result.returncode and not problems:
            problems = ['Backup status returned an unexpected error']
        if not problems:
            print('Backup watchdog: OK', flush=True)
            return 0
        message = '\n'.join(problems)
    except Exception as exc:
        message = 'Backup watchdog found a problem: ' + str(exc)
    print(message, file=sys.stderr, flush=True)
    content = f'iCloud backup needs attention — {dt.datetime.now(dt.UTC).isoformat()}\n\n{message}\n\nRun: icloud-backup status\n'
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.backup-warning-', dir=flag.parent)
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(content)
            os.replace(name, flag)
        finally:
            Path(name).unlink(missing_ok=True)
    except OSError as exc:
        print(f'Could not write Desktop warning: {exc}', file=sys.stderr)
    if sys.platform == 'darwin':
        script = 'on run argv\ndisplay notification (item 1 of argv) with title "iCloud Backup"\nend run'
        try:
            subprocess.run(['/usr/bin/osascript', '-e', script, message[:500]], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return 1


if __name__ == '__main__':
    os.umask(0o077)
    raise SystemExit(main())
