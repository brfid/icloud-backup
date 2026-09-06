# Configuration

The active configuration is a private TOML file selected by the installer. The default is `~/.config/icloud_backup/config.toml`; an existing installation's actual selection is discoverable through [operations](operations.md#identify-the-active-installation). [example.config.toml](../example.config.toml) is a template, never a live source inventory. The installer does not create or overwrite the active configuration.

`load_config()` and `Config` in [icloud_backup.py](../icloud_backup.py) define the accepted schema and defaults. Unknown settings, wrong types, unsafe path overlaps, and invalid source names are rejected. Paths must be absolute or start with `~`; they are resolved before overlap checks. Relative paths are not interpreted against the checkout.

## Backup store and sources

| Setting | Meaning |
| --- | --- |
| `dest_root` | Required existing-parent destination; the Restic backup store is its `restic-v2` child |
| `[[source]]` | At least one named directory to back up |
| `source.name` | Unique name, ignoring case: 1–64 ASCII letters/digits/underscores/dots/hyphens, starting with a letter or digit |
| `source.path` | Directory to capture; overlapping sources, destination, state, and cache are rejected |
| `source.excludes` | Optional list of case-sensitive component-name globs, such as `.git`, `*.pyc`, or `node_modules`; no slashes or empty patterns |
| `source.allow_empty` | Defaults to `false`; set `true` only when a completely empty source after exclusions is intentional |

Exclusions can match a file or directory name at any depth. Hidden files are otherwise included. Symlinks inside a source are preserved without following their targets. Git ignore rules are not read. Excluding `.git` and `*.git` is an example policy, not an engine-wide requirement; configure any separately named Git stores explicitly if you also want them excluded. Working files remain eligible for dated recovery independently of their Git status.

An empty or missing source fails visibly. Legacy `.filename.icloud` placeholders must be downloaded in Finder before retrying. Native dataless files are read normally, allowing macOS to download them on demand. See [architecture](architecture.md#icloud-and-space) for the tested behavior and limits.

## Retention

| Setting | Default | Allowed values |
| --- | --- | --- |
| `retention.daily` | `14` | Positive integer |
| `retention.weekly` | `8` | Positive integer |
| `retention.monthly` | `24` | Positive integer |

Restic retains the union of these calendar policies for each original source path, plus the latest point. A point can satisfy multiple periods. Buckets use UTC, including ISO weeks. Empty periods do not consume a slot, so sparse backups can span longer than two calendar years. Restic may additionally keep the oldest point when a requested count is not yet available. Multiple successful runs on the same UTC day normally leave that day's latest point. Renaming a source label does not change grouping; changing its path creates a separate history group.

Removing a source from configuration stops new captures. Existing managed history remains subject to the same retention policy, grouped by its original path; it is not automatically deleted wholesale. Failure and cleanup safeguards are owned by [architecture](architecture.md#completion-and-retention).

## Runtime options

| Setting | Default | Meaning |
| --- | --- | --- |
| `runtime.restic` | `/opt/homebrew/bin/restic` | Absolute executable path; change for Intel Homebrew or another installation |
| `runtime.reserve_mib` | `1024` | Minimum immediately free space checked during engine operations; integer ≥ 0 |
| `runtime.timeout_hours` | `6` | Maximum time for one engine command; positive integer |
| `runtime.state_dir` | `~/Library/Application Support/icloud-backup/state` | Local status, repository identity, operation lock, and command logs |
| `runtime.cache_dir` | `~/Library/Caches/icloud-backup/restic` | Disposable local Restic metadata cache |
| `runtime.flag_path` | `~/Desktop/⚠️ BACKUP FAILING.txt` | Visible failure/staleness warning |

The scheduled times and job names are deployment settings described in [operations](operations.md#schedule-and-alerts), not TOML retention or runtime settings. There is no password setting: all engine calls use the empty-password mode described in the README.

## Changes and precedence

The installed shortcut and jobs pass an explicit `--config` argument; they use that file on every invocation. When invoking the source script directly, precedence is explicit `--config`, then `ICLOUD_BACKUP_CONFIG`, then the default path. The installer itself uses `--config` or the default path; it does not consult that environment variable. An explicit later `--config` argument can override the installed shortcut's selection for an individual command.

Source changes make the next scheduled due check request a backup. Other changed settings apply on subsequent invocations but do not themselves force another successful backup that day. Reinstall if the configuration file's location changes. The exported `config-at-install.toml` is refreshed only by installation; it is a recovery copy, not active configuration. Follow the existing-install update procedure to refresh it while preserving job names.

Changing `state_dir` does not move the old state or repository identity. Treat that as a state migration; a normal run will refuse an unregistered identity. Changing the destination likewise does not copy the existing backup store. For an existing store, use the documented `connect` flow after placing it at the intended location.
