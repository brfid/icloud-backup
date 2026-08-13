# icloud_backup

Datetime-stamped, compressed, self-contained backups into a local iCloud Drive folder — no cloud API, token, or credential to maintain. You configure a list of folders; once a day a launchd agent writes one `.tar.gz` per folder into your iCloud `backups` directory, and Apple's own sync carries them off-machine. Old archives are pruned on a grandfather-father-son schedule, and a watchdog shouts if backups ever silently stop.

It is deliberately boring: every archive is a full, independent snapshot, so restoring is a single `tar` extract and pruning is a plain file delete. There are no delta chains to corrupt and no repository format to understand.

## Why

Most "back up to the cloud" setups lean on a provider API (Google Drive, S3, Dropbox) that needs OAuth tokens or keys kept alive. This one leans on a folder. If you already pay for iCloud storage, your Mac already syncs a local directory to it — so a backup is just *a compressed file that lands in that directory*. Nothing to authorize, nothing to renew.

## How it works

- **Full snapshots.** Each run writes `dest_root/<name>/<UTC-stamp>.tar.gz` for every configured source.
- **Skip-if-unchanged.** Before archiving, the source is fingerprinted (relative path + size + mtime of every non-excluded file). If it matches the last snapshot, nothing is written — only a heartbeat is updated. Storage and upload churn then track real changes, not the clock.
- **GFS retention.** Keep the newest snapshot per day for `daily` days, per week for `weekly` weeks, and per month for `monthly` months. The most recent snapshot is always kept.
- **Fail loudly.** If any source fails, you get a macOS notification and a `⚠️ BACKUP FAILING.txt` on your Desktop; a clean run clears it. Because a job cannot report its own *absence*, a separate `watch.py` runs at login and periodically, and alarms if any source has no successful backup within `stale_hours`.

## Requirements

Python 3.11+ (3.11 for the standard-library `tomllib`); Python 3.14+ only if you choose `compression = "zst"`. macOS, for the launchd agents and notifications. No third-party packages.

## Configure

Copy `example.config.toml` to `~/.config/icloud_backup/config.toml` and edit it. Each `[[source]]` has a `name`, a `path`, and optional `excludes` (component-name globs like `node_modules`, `*.pyc`, `.venv`; matching directories are pruned from the walk). See the example for a per-source retention override.

## Use

```
icloud_backup.py run              # snapshot every source (this is what launchd runs)
icloud_backup.py list             # show archives per source, and which would be pruned
icloud_backup.py verify           # reopen every archive and confirm it is readable
icloud_backup.py restore NAME     # extract the newest snapshot of NAME to ~/Desktop
icloud_backup.py restore NAME --at 20260813 --target /tmp/out
```

Point at a non-default config with `--config PATH` or `$ICLOUD_BACKUP_CONFIG`.

## Schedule

Copy the templates in `launchd/` to `~/Library/LaunchAgents/`, edit the `Label`, interpreter path, and script paths, then load them:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<your-label>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<your-label>-watch.plist
```

The backup runs daily at 03:00 (a missed run fires on next wake); the watchdog runs at login and every six hours.

## Limitations

- The fingerprint uses size + mtime, like `rsync`'s default — a change that preserves both (rare) is not detected. Delete the source's state file in `~/.local/state/icloud_backup/` to force a fresh snapshot.
- Off-machine safety depends on iCloud actually syncing. The tool writes locally and cannot confirm the bytes left your Mac; keep an eye on iCloud's status.
- Archives are not encrypted. If the data is sensitive, enable Apple's Advanced Data Protection (end-to-end iCloud encryption) or encrypt the archives yourself.
- Excludes are component-name globs, not full gitignore path patterns.

## License

MIT — see `LICENSE` (set your name in it before publishing).
