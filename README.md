# iCloud Backup

Incremental backups of configured macOS folders to iCloud Drive. Restic owns snapshot storage, integrity checks, and daily/weekly/monthly retention. A small Python runner owns local safeguards and scheduling; an independent watchdog reports failures.

This project deliberately uses Restic's **empty-password mode**. There is no backup password or separate recovery secret to manage; anyone with access to the complete backup store can recover its contents. It relies on your Mac and iCloud access controls. Reported success means local creation and checks passed; iCloud upload completion is not monitored.

## New installation

Requirements: macOS with iCloud Drive signed in, Python 3.11 or later, and Restic 0.19.1 or later. Homebrew can supply the dependencies with `brew install python restic`.

Copy [example.config.toml](example.config.toml) to `~/.config/icloud_backup/config.toml` and replace its example sources with your real folders. Keep that private configuration outside Git. Then, from this checkout:

```sh
python3 install.py --initialize
~/bin/icloud-backup status
```

Installation copies the programs to a stable location, loads the daily job and independent watchdog, and starts the first backup when due. The installed command and jobs use the same explicit configuration path. Deleting the checkout does not disable them.

**Already installed?** Follow [the update procedure](docs/operations.md#update-an-existing-installation), preserving the configuration path and job labels reported by the current installation. Editing this checkout alone does not update the running installation.

## Documentation

| Need | Owner |
| --- | --- |
| Configure sources, exclusions, retention, and runtime limits | [Configuration](docs/configuration.md) and its executable example above |
| Find the live configuration, installed copies, state, and scheduler; update or operate them | [Operations](docs/operations.md) |
| Understand component boundaries, completion, and retention safeguards | [Architecture](docs/architecture.md) |
| Recover after losing the original Mac | [RECOVERY.txt](RECOVERY.txt), also exported beside the backup store |
| Understand the storage tradeoffs | [Storage decision](docs/adr-001-storage.md) |
| Make a change as a coding agent | [AGENTS.md](AGENTS.md) |

Tests use temporary repositories: `python3 -m unittest discover -s tests -v`. Validation scope and limits are documented in [architecture](docs/architecture.md#validation). The project is [MIT licensed](LICENSE).
