# iCloud Backup

Daily, incremental backups of configured folders to iCloud Drive, using Restic for storage, snapshots, integrity checks, and retention. A small Python runner handles scheduling, source checks, status, and safe restore destinations. No password, keychain item, or external recovery secret is required.

## Requirements and installation

Use macOS with iCloud Drive signed in, Python 3.11 or later, and Restic 0.19.1 or later. Homebrew's stable executable paths are used on installation. Install dependencies with `brew install python restic` if needed.

Copy `example.config.toml` to `~/.config/icloud_backup/config.toml` and edit the sources. Each `[[source]]` has a unique name, a directory path, and optional component-name exclusion globs. Ordinary hidden files and symbolic links are included; symlink targets are not followed. The example excludes Git metadata, so snapshots preserve working files rather than commit history. Git ignore rules are not read. Sources must not overlap each other, the destination, local state, or cache.

```sh
python3 install.py --initialize
~/bin/icloud-backup status
```

The installer creates `restic-v2` inside `dest_root`, installs a copy of the runner and an independent watchdog under `~/Library/Application Support/icloud-backup/runtime`, and installs `~/bin/icloud-backup`. Deleting this development checkout does not disable the installed programs. The private configuration remains outside the checkout. Prior installed files are saved under `~/Library/Application Support/icloud-backup/migration/` before replacement.

The default launchd labels are `local.icloud-backup` and `local.icloud-backup-watch`. Use `--label` to retain an existing installation's base label. The daily job runs at 03:00 local time and at login; an hourly due check catches failures or missed runs without creating another successful daily run. A separate watchdog runs at login and every six hours. These user jobs run while the account is logged in and catch up when it next runs after sleep or shutdown. The first backup starts on installation. Allow required macOS file access if macOS requests it, then confirm all configured sources have completed through the scheduled job.

After changing code, rerun the installer to update the stable runtime. After changing source configuration, the next due check uses it; rerun the installer to refresh its recovery copy in iCloud. For an existing repository on another Mac, update the paths, run `icloud_backup.py connect`, then install without `--initialize`. `connect` reads and checks all stored data before registering the repository locally. A normal backup never recreates a missing repository.

## Daily use

```sh
icloud-backup status
icloud-backup snapshots
icloud-backup run
icloud-backup verify
icloud-backup verify --full
icloud-backup restore projects --snapshot SNAPSHOT_ID --target "$HOME/Downloads/recovered-projects"
```

Use `~/bin/icloud-backup` if `~/bin` is not on your shell's search path. `run` forces a backup; the scheduler uses `tick` to decide whether one is due. `status --json` gives structured local status. `snapshots` lists checked recovery points by ID, time, and source. Omit `--snapshot` to restore the latest checked version. Restores require a new or empty directory outside all sources and backup storage and verify recovered contents. They do not overwrite the live source. A deleted file can be recovered from a snapshot taken before deletion.

An empty source is an error unless explicitly configured with `allow_empty = true`. This prevents an unavailable or unexpectedly empty folder from replacing good history. A source containing a legacy `.filename.icloud` placeholder must first be downloaded in Finder. Native dataless files are supported through macOS's normal download-on-read behavior.

## Retention and checking

The default policy keeps 14 daily, 8 weekly, and 24 monthly recovery points for each original source path. Restic applies the union of these policies: a point can satisfy several periods. Calendar buckets use UTC, including ISO weeks. Empty periods do not use up the count, so sparse backups can span longer than two calendar years. Restic may additionally retain the oldest point when a requested count is not yet available. The latest point is always kept. Repeated manual runs on the same day normally replace that day's earlier point during retention.

Each source is backed up separately. A run first writes snapshots tagged `pending`, then checks the repository before marking successful sources `complete`. The first run, runs after failures, and runs at least 30 days after the last full check read all stored data. Other runs check all repository structure and read one of seven data-pack subsets, rotating by date. This daily sample is not a reread of every byte. Restic also verifies data before storing it; restores use `--verify`.

Missing sources, failed reads, interrupted operations, failed checks, and the free-space guard are errors. Healthy sources can still gain checked snapshots when another source fails, but no retention is applied that run. A failed or incomplete snapshot is never offered as a completed restore point. Earlier pending snapshots are removed only after a checked replacement succeeds. A local process lock prevents concurrent backup, restore, and maintenance commands. Restic's stale locks are cleared at the next run; active locks are never forcibly removed.

Retention selects only this app's `icloud-backup-v2,complete` snapshots. Once a full check passes, monthly cleanup removes wholly unused storage files with `prune --max-repack-size 0`; shared data files are not repacked. Unused bytes inside shared packs can therefore remain, trading some cloud space for less rewriting and downloading. Revisit this choice if repository growth becomes a problem. Data made obsolete between cleanups can remain until the next cleanup. Legacy archives and other folders in `dest_root` are outside this policy.

## iCloud and local space

The repository is directly in iCloud Drive. There is no permanent local copy of the complete repository outside iCloud. Restic uses a disposable local metadata cache under `~/Library/Caches/icloud-backup/restic`. iCloud may remove downloaded backup data and fetch it when Restic needs to read it. Listing a folder, examining metadata, or changing a timestamp does not download its contents; no `touch` sweep is used.

This behavior was tested on macOS 26.6.2 with Restic 0.19.1: listing snapshots, adding an incremental backup, restoring old and deleted files, full checking, and pruning worked after native eviction. A normal incremental run left most old data packs evicted. Reading cloud-only source data also worked. This establishes the tested local behavior; a sync folder still does not offer a transactional remote repository.

The app reports local creation and checks. It assumes iCloud will sync and does not monitor upload progress, quotas, or remote availability. A new snapshot may not be available on another Mac immediately. Keep one Mac as the writer and allow syncing to finish before remote recovery. If iCloud has a problem, fix it through iCloud and retry. Finder's Download Now is a fallback when reads cannot materialize required files; there is no need to permanently pin the entire repository.

Initial backups, full checks, and restores can download considerable data. iCloud eviction has no deadline. The runner stops if immediately free space falls below 1 GiB by default; Finder's available space can include reclaimable space and show a larger number. The guard checks during engine operations and fails visibly instead of deleting backups or source data to make room. A restore needs room for both downloaded repository data and restored files. Individual engine commands have a six-hour limit; a stopped run can be retried.

## Alerts and logs

`status` reports each source's latest locally checked point, source/configuration problems, interrupted or long-running work, missing runtime files, and unloaded scheduled jobs. The watchdog creates `~/Desktop/⚠️ BACKUP FAILING.txt` and attempts a macOS notification for failures or a source more than 36 hours stale. The warning is cleared after a successful complete run. Notifications depend on macOS settings; the Desktop file is a second signal.

The watchdog is a separate program and can report a missing or broken runner. It still depends on this Mac being on and logged in, Python working, and its own job being present. No local checker can warn while the Mac is off or after all its jobs are removed. `status` can reveal removed jobs when run manually.

Main logs are `~/Library/Logs/icloud-backup.log` and `~/Library/Logs/icloud-backup-watch.log`. The last 40 individual Restic command logs and private status are under `~/Library/Application Support/icloud-backup/state/`. No public repository should contain those files, private configuration, source inventories, or recovery copies. Commands inherit no `RESTIC_*` environment settings from your shell.

## Recovery after losing the Mac

Sign in to iCloud on a replacement Mac and install Restic. The installer puts [RECOVERY.txt](RECOVERY.txt) and a configuration copy in `dest_root/restic-v2-recovery`, outside the repository so they can be read without the app. That guide shows how to list and restore snapshots directly using Restic. The original app, local state, metadata cache, and original username are not required. Use the original path recorded in the chosen snapshot when restoring a subtree.

Restic always uses its encrypted storage format, but this installation uses the officially supported empty password with `--insecure-no-password` on every command. Anyone who can read the repository can recover its data. Mac/iCloud access controls provide the privacy you chose. Keep the entire repository, including `config` and `keys/`: they are necessary internal metadata, with no separate secret for you to save.

Backups copy files as they are read; they are not application-consistent database exports or an atomic snapshot of all live files. Quit a game before a backup when you need a consistent world, and use an application's database export or a quiet period for active databases. The four-source file backup is not a complete operating-system image.

## Migration, stopping, and validation

The prior custom archive implementation and its tests remain in Git history. The new repository has a separate name and does not adopt or delete old `.tar.gz` archives. Old archives remain directly extractable into a separate empty folder using standard archive tools. Preserve them until their recovery is independently demonstrated.

To stop scheduling, unload both installed launchd labels with `launchctl bootout gui/$(id -u)/LABEL`, substituting the actual backup and watchdog labels. This does not delete backup data. Reinstall to load them again. To uninstall files, first stop both jobs and preserve the configuration, recovery guide, and iCloud repository; removing the runtime and CLI does not remove the backups.

Run `python3 -m unittest discover -s tests -v`. Tests use temporary repositories and check real empty-password restore of older and deleted files, hidden files and symlinks, Git exclusions, missing sources, partial read failures, integrity/space failures, process interruption and recovery, unrelated snapshot protection, 14/8/24 retention across year/leap boundaries, separate source groups, scheduler wiring, missing-runner alerts, and operation after a fixture development checkout is deleted.

See [the storage decision](docs/adr-001-storage.md) for the rationale and future tradeoffs.
