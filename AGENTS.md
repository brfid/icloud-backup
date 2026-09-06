# Working on iCloud Backup

Read [README.md](README.md), then the relevant document in its ownership table. Keep each procedure or explanation in its owner and link to it elsewhere. Python docstrings describe responsibilities, invariants, and side effects; they do not repeat the operating manual. Use soft-wrapped Markdown.

## Source and installation are separate

This checkout is the portable source of truth for code and documentation. It is not the active installation and contains no authoritative machine configuration. Before working on a live installation, follow [Identify the active installation](docs/operations.md#identify-the-active-installation). The installed command's JSON status and private configuration determine the actual sources, paths, and job labels; the example file does not.

Edit code here, then use the documented installer when deployment is part of the task. Preserve the discovered configuration and label arguments. Do not edit generated runtime copies or plists directly, replace a live config with the example, or run new-install initialization as an update. A docs-only edit does not itself require restarting scheduled jobs.

Keep machine inventories, private configuration, status output, logs, recovery copies, restored data, and publication-review notes outside Git. Do not copy them into these instructions. `.gitignore` reduces accidental additions; it does not sanitize tracked files or history. Changing repository visibility or rewriting existing history requires an explicit user request.

## Maintain the boundaries

Follow [the component responsibilities and invariants](docs/architecture.md). Let Restic own storage and retention. Keep the watchdog independent of imports from the runner so it can report a missing or broken runner; its small fallback notification/file-writing code is intentional duplication.

Use temporary folders and an explicit test configuration for experiments. Do not exercise destructive or failure tests against a live backup store. For behavioral changes, run the tests that cover the affected contract and keep the configuration example/reference consistent with parser changes. For docs or docstring changes, check links, Markdown formatting, and CLI help as appropriate; do not start a real backup merely to test prose.
