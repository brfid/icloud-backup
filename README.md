# icloud_backup

icloud_backup writes compressed, timestamped snapshots of folders you choose into a folder inside iCloud Drive. iCloud syncs those snapshots to Apple's servers and your other devices, so backups leave your Mac with no cloud API, access token, or password to maintain.

Each run creates one `.tar.gz` per source folder. The tool removes older snapshots on a daily, weekly, and monthly schedule, and reports when a backup fails or stops running.

## Requirements

- macOS, for the launchd agents and notifications.
- Python 3.11 or later. Python 3.14 or later is required only for `compression = "zst"`.
- No third-party packages.

## Install

1. Clone this repository, for example to `~/src/icloud-backup`.
2. Copy `example.config.toml` to `~/.config/icloud_backup/config.toml` and edit it. See [Configure](#configure).
3. Run one backup to confirm it works:

   ```
   python3 icloud_backup.py run
   ```

To run backups automatically, see [Schedule backups](#schedule-backups).

## Configure

The config file sets a destination, a compression format, a retention policy, and one or more sources.

```toml
dest_root = "~/Library/Mobile Documents/com~apple~CloudDocs/backups"
compression = "gz"   # "gz" (Python 3.11+) or "zst" (Python 3.14+)
stale_hours = 36     # the watchdog alarms if a source has no backup within this window

[retention]
daily = 7
weekly = 4
monthly = 12

[[source]]
name = "documents"
path = "~/Documents"
excludes = [".DS_Store", "node_modules", "*.pyc"]
```

Each `[[source]]` accepts these keys:

- `name`: the subfolder under `dest_root` that holds this source's snapshots.
- `path`: the folder to back up.
- `excludes` (optional): component-name globs. The tool skips any file or directory whose name matches, and does not descend into a matching directory.
- `retention` (optional): a per-source override of the global `[retention]` table.

## Use

Invoke each command as `python3 icloud_backup.py <command>`. Pass `--config PATH`, or set `ICLOUD_BACKUP_CONFIG`, to use a config other than `~/.config/icloud_backup/config.toml`.

| Command | Description |
| --- | --- |
| `run` | Back up every source. Add `--only NAME` to back up one source. |
| `list` | Show each source's snapshots, their sizes, and which ones the next prune keeps. |
| `verify` | Reopen every snapshot and report whether it is readable. |
| `restore NAME` | Extract a snapshot. Defaults to the newest; use `--at STAMP` to pick one and `--target DIR` to set the output location. |

For example, to restore a specific snapshot to a scratch directory:

```
python3 icloud_backup.py restore documents --at 20260813 --target /tmp/restore
```

## Schedule backups

The `launchd/` directory holds two agent templates:

- `com.example.icloud-backup.plist` runs a backup daily at 03:00.
- `com.example.icloud-backup-watch.plist` runs the watchdog at login and every six hours.

Copy each template to `~/Library/LaunchAgents/`, set the `Label`, the Python interpreter path, and the script path, then load it:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

## How it works

### Snapshots

Each snapshot is a complete `.tar.gz` of the source. Snapshots are independent, so a restore is a single extract and a prune is a file deletion. The archive's top-level directory is the source folder's own name.

### Skip-if-unchanged

Before archiving, the tool fingerprints the source from the relative path, size, and modification time of every non-excluded file. When the fingerprint matches the previous snapshot, the tool writes no new archive and records a heartbeat instead. Storage and upload activity then track how often the source changes rather than how often the tool runs.

### Retention

A prune keeps the newest snapshot in each of the most recent `daily` days, `weekly` weeks, and `monthly` months, and always keeps the most recent snapshot.

### Failure reporting

When a source fails, `run` posts a macOS notification, writes a file named `⚠️ BACKUP FAILING.txt` to the Desktop, and exits with a non-zero status. A later successful run removes the file.

A scheduled job cannot report that it never started. The watchdog, `watch.py`, handles that case: it runs on its own agent and reports any source whose last successful backup is older than `stale_hours`. The tool records per-source state in `~/.local/state/icloud_backup/`.

## Limitations

- The fingerprint compares size and modification time, like rsync's default. It does not detect a change that preserves both. To force a fresh snapshot, delete the source's state file in `~/.local/state/icloud_backup/`.
- The tool writes to a local folder and cannot confirm that iCloud uploaded it. Off-site durability depends on iCloud syncing.
- Snapshots are not encrypted. For encryption at rest, turn on Advanced Data Protection or encrypt the archives yourself.
- Excludes match single path components, not full path patterns.

## License

MIT. See [LICENSE](LICENSE).
