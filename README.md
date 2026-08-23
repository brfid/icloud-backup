# icloud_backup

`icloud_backup` creates one complete, timestamped `.tar.gz` snapshot of every configured folder and writes it to iCloud Drive. With the documented 7/4/12 policy, a successful daily run keeps recovery points from the seven most recent represented UTC dates, four most recent represented ISO weeks, and twelve most recent represented UTC months.

Each archive is independent and can be opened with Finder or standard `tar`; recovery never depends on this repository. The tool also reports a failed run immediately and uses an independent watchdog to report when scheduled snapshots stop appearing.

## Requirements

- macOS, for iCloud Drive, launchd scheduling, and notifications.
- Python 3.11 or later.
- No third-party Python packages.

## Quick start

1. Clone this repository and enter it, for example:

   ```sh
   mkdir -p ~/src
   git clone https://github.com/brfid/icloud-backup.git ~/src/icloud-backup
   cd ~/src/icloud-backup
   ```

2. Create the configuration directory, copy the example, and edit the destination and sources:

   ```sh
   mkdir -p ~/.config/icloud_backup
   cp example.config.toml ~/.config/icloud_backup/config.toml
   ```

3. Create and inspect the first snapshots:

   ```sh
   python3 icloud_backup.py run
   python3 icloud_backup.py status
   ```

4. Install the two launchd agents described in [Schedule backups](#schedule-backups).

## Upgrade from earlier versions

This release intentionally replaces skip-if-unchanged snapshots, JSON state, configurable compression, per-source retention, and repository-driven restore commands with complete daily gzip-tar snapshots whose filenames are the durable state.

Before the first upgraded `run`:

1. Remove `compression`, `stale_hours`, and every per-source `retention` value from the installed config. The strict schema rejects these obsolete keys instead of silently ignoring them.
2. Existing timestamped `.tar.gz` archives remain compatible. For each important legacy gzip archive that is already local, run `gzip -t "/path/to/archive.tar.gz" && tar -tzf "/path/to/archive.tar.gz" >/dev/null` once before allowing the upgraded run to prune it. Do not bulk-open cloud-only archives unless you intend to download them.
3. Legacy `.tar.zst` archives are left untouched but are not reported or pruned by this version. Keep them separately and retain a zstd-capable tool for restoring them.
4. The old state under `~/.local/state/icloud_backup/` is no longer read. It may remain without affecting backups.
5. Replace both installed launch-agent copies with the updated templates, reapply the local paths and labels, and reload them as described in [Schedule backups](#schedule-backups). A Git pull does not update files already copied into `~/Library/LaunchAgents/`.

The old `list`, `verify`, and `restore` commands and the `run --only` option no longer exist. Use `status`, Finder or standard `tar`, and full all-source runs instead. Run `status` and one manual `run` successfully before relying on the schedule again.

## Configure

The configuration contains one destination, one global retention policy, and one or more sources:

```toml
dest_root = "~/Library/Mobile Documents/com~apple~CloudDocs/backups"

[retention]
daily = 7
weekly = 4
monthly = 12

[[source]]
name = "documents"
path = "~/Documents"
excludes = [".DS_Store", "node_modules", "*.pyc"]
```

The schema is strict: unknown keys are errors, so a misspelled or obsolete setting cannot be silently ignored.

- `dest_root` is the folder that will contain one archive subfolder per source.
- `retention.daily`, `retention.weekly`, and `retention.monthly` are required positive integers and apply to every source.
- `source.name` must be unique and must be a simple name, not `.`, `..`, or a value containing a path separator.
- `source.path` must name the directory to snapshot.
- `source.excludes` is optional. Each value is a component-name glob; a matching file or directory is skipped, and the tool does not descend into a matching directory.

Paths beginning with `~` are expanded and every path is resolved before use. Other relative paths are rejected. Source directories must not overlap one another or the destination tree; these checks prevent duplicate coverage and prevent an archive from ingesting previous backups or its own partial output.

The default config path is `~/.config/icloud_backup/config.toml`. To use another file, put `--config` before the command or set `ICLOUD_BACKUP_CONFIG`:

```sh
python3 icloud_backup.py --config /absolute/path/config.toml status
```

## Use

`icloud_backup` has two user-facing commands:

- `python3 icloud_backup.py run` snapshots every configured source, then applies retention after each successful publication. A nonblocking process lock prevents manual and scheduled runs from overlapping.
- `python3 icloud_backup.py status` reports each source’s newest snapshot, its age, the archive count, and the daily, weekly, or monthly reasons each archive is retained. It reads filenames rather than archive contents, so checking status does not download cloud-only archives.

Every run creates a new full snapshot for every source, even when its contents have not changed.

## Restore files

Choose a timestamped `.tar.gz` under `dest_root`, then copy or download it to a recovery folder outside the backup tree before opening that copy with Finder and Archive Utility. Archive Utility normally extracts beside the archive; using a separate recovery folder avoids placing an extracted source tree among the managed backups. Inspect the result before copying back the files you need.

The equivalent standard `tar` command is:

```sh
mkdir -p ~/Desktop/icloud-backup-recovery
tar -xzf "/path/to/20260823T030000Z.tar.gz" -C ~/Desktop/icloud-backup-recovery
```

The archive contains the source folder as its top-level directory. If Optimize Mac Storage has evicted the archive’s local contents, opening or extracting it causes macOS to download it first.

## Schedule backups

The `launchd/` directory contains two agent templates:

- `com.example.icloud-backup.plist` runs `run` daily at 03:00.
- `com.example.icloud-backup-watch.plist` runs the watchdog at login and every six hours.

Copy both templates to `~/Library/LaunchAgents/`, set their labels and repository paths, and optionally add `ICLOUD_BACKUP_CONFIG` inside `EnvironmentVariables`. A launchd config value must be an absolute path such as `/Users/USERNAME/.config/icloud_backup/config.toml`; launchd does not expand `~`. Ensure Python 3.11 or later is available on each template’s configured `PATH`, then load each agent:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

To replace an agent that is already loaded, unload it before copying the edited replacement and load it again afterward:

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

The templates start with Apple’s stable `/usr/bin/env` and resolve `python3` through a `PATH` covering Apple Silicon Homebrew, Intel Homebrew or python.org, and system locations. Replacing a Python installation therefore does not invalidate launchd’s cached executable requirement. If the Mac is asleep at 03:00, launchd starts one current run after wake; it cannot recreate snapshots for the time it was asleep.

## Safety model

### Full verification and publication

For each source, `run` first confirms that the source is a readable directory and still satisfies the path-safety rules. It then creates a uniquely named hidden partial file in the final destination directory, adds the source tree once while applying exclusions, flushes and syncs the file, and fully reads the gzip stream to EOF to check its CRC and length. It also requires a readable, nonempty tar member list.

Only after those checks pass does the tool atomically rename the partial file to `<UTC timestamp>.tar.gz` and sync the destination directory. A final archive published by this version has therefore completed local gzip and tar checks. A failure before publication removes only the partial file; a failure after publication leaves the verified final archive untouched. No archive-creation or publication failure prunes or replaces a completed archive.

### Retention

Retention processes recognized archive timestamps newest first and keeps the newest archive from each of the seven most recent represented UTC dates, four most recent represented ISO weeks, and twelve most recent represented UTC calendar months. It also keeps the newest archive unconditionally and takes the union of all four sets.

The documented 7/4/12 policy keeps at most 23 archives per source and normally fewer because one snapshot can represent its date, week, and month. With other positive retention counts, the upper bound is their sum. These are represented calendar buckets, not exact ages: missed weeks or months can leave an older recovery point without increasing the configured limit.

Pruning starts only after a new archive has passed every publication check. Partial files and unrecognized filenames are ignored. If a recognized archive timestamp is implausibly in the future, all destructive cleanup is refused until the clock or filename is corrected. If a deletion fails, the extra completed archive remains, the failure is reported, and cleanup is retried on a later run.

### Alarms and watchdog

On an error, `run` writes a log entry, posts a macOS notification, writes `⚠️ BACKUP FAILING.txt` on the Desktop, and returns a nonzero status. If the Desktop alarm itself cannot be written, that failure is printed to the launchd log or terminal without hiding the original backup error. The independent watchdog uses the same archive-based health calculation as `status` and raises the alarm if an archive directory cannot be read, a source has never completed a snapshot, its newest timestamp is implausibly in the future, or its newest snapshot exceeds the fixed 36-hour threshold.

The watchdog never clears the Desktop alarm. A `run` clears it only when every source published a checked snapshot during that run, all retention cleanup succeeded, and no other error occurred, so one healthy source cannot hide another source’s failure.

## iCloud and format limitations

- A daily schedule can leave up to roughly one day of changes between snapshots.
- The tool confirms the completed local archive, but it cannot confirm that iCloud uploaded it. Check Finder’s iCloud Status and periodically perform a recovery from iCloud.com or another device for an end-to-end test.
- Gzip provides compression, not encryption. This tool does not add archive encryption; use the iCloud account protections appropriate for the sensitivity of the source data.
- Optimize Mac Storage may evict completed archives when local space is needed, but it neither deletes the cloud copy nor guarantees immediate eviction. Reading cloud-only source files materializes them locally, and creating a new archive requires enough transient local space for one complete snapshot.
- Keep each source comfortably below iCloud Drive’s individual-item size limit because each snapshot is one file.
- A source can change while it is read, so an archive is not an atomic filesystem snapshot. Schedule runs while sources are normally quiet.
- Standard tar preserves ordinary files, directories, permissions, and symlinks, but it is not a full macOS system-backup format; application-specific extended attributes, ACLs, and other metadata may require separate protection.
- The archives help recover unwanted changes and deletions, but sources and snapshots in the same iCloud account still share an account and synchronization domain.
- Historical archives are not routinely reopened because doing so can download evicted iCloud content. Filename-only status cannot detect later corruption, manual replacement, or an unverified legacy file with a valid timestamp name. Archives published by this version are fully checked before publication; periodic end-to-end recoveries provide the operational check after upload.
- Path-safety checks assume other processes running as the same user are not deliberately swapping source or destination ancestors during a backup. That user can already alter the source and completed archives directly.

## License

MIT. See [LICENSE](LICENSE).
