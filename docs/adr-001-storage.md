# ADR-001: Password-free Restic repository in iCloud Drive

**Status:** Accepted

**Date:** 2026-09-05

## Context

The previous implementation wrote complete compressed archives and maintained custom retention. The desired replacement needs incremental storage and calendar history, limited local space, and no additional password. Downloading cloud data during recovery is acceptable. iCloud syncing is trusted; a separate upload monitor is outside the intended scope.

## Decision

Use Restic's supported empty-password mode with one backup store directly in iCloud Drive, plus disposable local metadata cache. Use native download-on-read behavior. A small installed runner coordinates local operations; a separate watchdog can report runner failures. Restic owns storage and retention. The current mechanics and tested limits belong to [architecture](architecture.md), and current settings belong to [configuration](configuration.md).

## Options considered

| Option | Benefit | Cost or limitation |
| --- | --- | --- |
| Full compressed archives | Independently readable dated files | Repeated full writes and cloud storage; custom retention |
| Restic directly in iCloud | Mature incremental storage and retention; tested native eviction behavior | Shared store must remain intact; remote syncing is asynchronous |
| Complete local store plus upload copy | Separates local operation from cloud transfer | Permanent local space and extra transfer/recovery machinery |
| Another snapshot engine | Mature alternatives exist | Each engine needs separate eviction and consistency validation |

## Consequences

Recovery needs the complete backup store but no separately remembered secret. Users rely on Mac/iCloud access controls for privacy. Local completion cannot assert remote availability. Avoiding repacks reduces shared-file rewriting and downloads while permitting some obsolete bytes to remain. These are deliberate tradeoffs rather than a transactional guarantee from iCloud.

Revisit cleanup if wasted cloud space becomes significant. Revisit the destination if multiple writers, independently confirmed off-device completion, or strict remote consistency become requirements. The [operations guide](operations.md) owns installation and recovery procedures; this decision record is not a second runbook.

## References

- [Restic repositories with empty passwords](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html#repositories-with-empty-password)
- [Restic retention](https://restic.readthedocs.io/en/stable/060_forget.html)
- [Apple: Getting ready for dataless files](https://developer.apple.com/documentation/technotes/tn3150-getting-ready-for-data-less-files)
