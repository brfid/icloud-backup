# icloud_backup

`icloud_backup` writes an independent, timestamped `.tar.gz` archive of each configured source to a destination, typically iCloud Drive. Every successful source backup creates a full, non-incremental archive subject to configured exclusions; recovery requires only Finder or `tar`.

After installation, the included `launchd` templates schedule daily backups and separate watchdog checks. Retention rotates daily, weekly, and monthly recovery points. Completed archive filenames are the backup record; the tool uses only the Python standard library and keeps no backup database.

## Requirements

- macOS.
- Python 3.11 or later.

## Quick start

1. Clone the repository:

   ```sh
   git clone https://github.com/brfid/icloud-backup.git
   cd icloud-backup
   ```

2. Copy the example configuration, then edit `dest_root` and `[[source]]`:

   ```sh
   mkdir -p ~/.config/icloud_backup
   cp example.config.toml ~/.config/icloud_backup/config.toml
   ```

3. Create and inspect the first archives:

   ```sh
   python3 icloud_backup.py run
   python3 icloud_backup.py status
   ```

4. To automate backups, [schedule the launch agents](#schedule-backups).

## Configure backups

[example.config.toml](example.config.toml) defines the complete schema. The default config path is `~/.config/icloud_backup/config.toml`.

`dest_root` is the archive directory. The global `daily`, `weekly`, and `monthly` retention values must be positive integers. Each `[[source]]` requires a unique, simple `name` for its archive subdirectory and a `path` to back up. Optional `excludes` values are component-name globs; matches skip files or entire directory trees.

Configured destination and source paths must be absolute or begin with `~`. The tool resolves them and rejects overlapping sources, sources that overlap the destination, and unknown configuration keys.

To use another config, set `ICLOUD_BACKUP_CONFIG` or put `--config` before the command:

```sh
python3 icloud_backup.py --config /absolute/path/config.toml status
```

## Run backups and inspect status

- `python3 icloud_backup.py run` attempts a new full archive for every source, even when its contents have not changed. It prunes a source only after publishing its new archive. A nonblocking lock makes a concurrent run log a skip and exit with status 2.
- `python3 icloud_backup.py status` reports freshness, archive count, retention reasons, and health warnings without opening or downloading archive contents. Freshness or read problems exit 1; excess or unrecognized-filename warnings alone exit 0.

## Schedule backups

The `launchd/` directory contains two templates:

- `com.example.icloud-backup.plist` runs a backup daily at 03:00. If the Mac is asleep, `launchd` runs one current backup after wake; it does not backfill missed days.
- `com.example.icloud-backup-watch.plist` checks backup health at login and every six hours.

Copy both files to `~/Library/LaunchAgents/`. In each copy, set `Label`, replace every `/Users/USERNAME` path, and ensure the configured `PATH` resolves Python 3.11 or later. To use a nondefault config, add `ICLOUD_BACKUP_CONFIG` to both agents with an absolute path; `launchd` does not expand `~`.

Load each agent:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

To update a loaded agent, run `bootout`, replace the copied file, and run `bootstrap` again. Pulling this repository does not update installed copies.

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

## Restore files

Copy or download the selected `.tar.gz` archive to a recovery directory outside `dest_root`. Open the copy with Finder or extract it with `tar`. This keeps recovery output out of the managed backup tree.

```sh
mkdir -p ~/Desktop/icloud-backup-recovery
tar -xzf "/path/to/20260823T030000Z.tar.gz" -C ~/Desktop/icloud-backup-recovery
```

The archive contains the source folder as its top-level directory. Opening a cloud-only archive downloads it first.

## Safety model

### Archive publication

`run` writes, syncs, and fully validates a same-directory partial archive. It publishes and syncs the archive without replacing an existing file, then prunes. A failure before publication attempts to remove the partial. No publication failure replaces or prunes a completed archive.

### Retention

Retention keeps the newest archive from each of the configured number of represented UTC dates, ISO weeks, and UTC months, then takes the union. After successful pruning, the default 7/4/12 policy retains at most 23 recognized archives per source and normally fewer. These are represented calendar buckets, not age windows; a missed period can leave an older recovery point.

Pruning deletes only recognized timestamped archives and ignores partial and unrecognized filenames. A recognized timestamp more than five minutes in the future blocks cleanup. A cleanup failure reports an error and can leave extra archives for a later run.

### Failure detection

A source failure is logged, triggers a best-effort macOS notification, and tries to write `⚠️ BACKUP FAILING.txt` on the Desktop. It does not stop the remaining sources, and the run exits nonzero. Only a completely successful run clears the file.

The separately scheduled watchdog raises the same alarm when it cannot check the config or archive directories, a source has no archive, the newest timestamp is more than five minutes in the future, or the newest archive is more than 36 hours old. Warnings for excess archives or unrecognized filenames alone do not alarm. The watchdog never clears the alarm.

## Limitations

- A healthy daily schedule can lose about one day of changes; sleep or failures can extend that interval.
- For an iCloud destination, local verification does not confirm upload. `status` and the watchdog read filenames only, so they cannot detect later corruption, replacement, or an unverified legacy archive. Periodically restore from iCloud.com or another device.
- Gzip and tar do not encrypt archives or preserve every macOS-specific metadata type.
- Sources that also sync through the destination's iCloud account share a failure domain with the archives.
- Each run needs transient local space for a full archive. For an iCloud destination, each archive must fit iCloud Drive's per-item limit, and macOS can evict its local contents. Reading cloud-only source files downloads them.
- A source can change while read, so an archive is not an atomic filesystem snapshot.

## Upgrade from versions with JSON state

Before the first upgraded `run`:

1. Remove `compression`, `stale_hours`, and source-level `retention` settings. The current schema rejects them.
2. Validate each important legacy `.tar.gz` that is already local before allowing retention to prune it. Do not bulk-open cloud-only archives unless you intend to download them.

   ```sh
   gzip -t "/path/to/archive.tar.gz" && tar -tzf "/path/to/archive.tar.gz" >/dev/null
   ```

3. Keep legacy `.tar.zst` archives separately; this version neither reports nor prunes them.
4. Replace both installed launch-agent files with the current templates, reapply local paths and labels, and reload them.

State under `~/.local/state/icloud_backup/` is ignored and can remain. The former `list`, `verify`, and `restore` commands and `run --only` option no longer exist; use `status`, Finder, or `tar`. Complete one manual `run`, then confirm `status` before relying on the schedule.

## License

MIT. See [LICENSE](LICENSE).
