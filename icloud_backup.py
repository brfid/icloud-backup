#!/usr/bin/env python3
"""Create and verify complete daily iCloud snapshots.

Each run writes one independent <source>/<UTC timestamp>.tar.gz archive for
every configured source. Completed archive filenames are the backup record; no
repository database or program-specific restore command is required.

Commands: run / status.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # Diagnose this before importing 3.11 modules.
    print("icloud_backup needs Python 3.11+", file=sys.stderr)
    raise SystemExit(2)

import argparse
import ctypes
import datetime as dt
import fcntl
import fnmatch
import gzip
import json
import os
import secrets
import stat
import subprocess
import tarfile
import tomllib
import unicodedata
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
ARCHIVE_SUFFIX = ".tar.gz"
STALE_HOURS = 36
FUTURE_TOLERANCE_HOURS = 5 / 60

DEFAULT_CONFIG = Path.home() / ".config/icloud_backup/config.toml"
DESKTOP_FLAG = Path(
    os.environ.get("ICLOUD_BACKUP_FLAG")
    or Path.home() / "Desktop" / "⚠️ BACKUP FAILING.txt"
)
RUN_LOCK = Path(
    os.environ.get("ICLOUD_BACKUP_LOCK")
    or Path.home() / "Library/Caches/icloud_backup/run.lock"
)


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class Retention:
    """How many represented UTC calendar buckets survive pruning."""

    daily: int
    weekly: int
    monthly: int


@dataclass(frozen=True)
class Source:
    """A named source directory and component-name exclusion patterns."""

    name: str
    path: Path
    excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """The destination, one global retention policy, and all sources."""

    dest_root: Path
    retention: Retention
    sources: tuple[Source, ...]

    def dest_for(self, name: str) -> Path:
        return self.dest_root / name


def _reject_unknown_keys(
    values: Mapping[str, object], allowed: set[str], context: str
) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise RuntimeError(f"unknown {context} key(s): {', '.join(unknown)}")


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"{label} must be a positive integer")
    return value


def _resolved_config_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} must be a non-empty path string")
    try:
        expanded = Path(value).expanduser()
    except (RuntimeError, KeyError) as exc:
        raise RuntimeError(f"cannot expand {label}: {value}") from exc
    if not expanded.is_absolute():
        raise RuntimeError(f"{label} must be absolute or start with '~': {value}")
    try:
        return expanded.resolve()
    except OSError as exc:
        raise RuntimeError(f"cannot resolve {label}: {value}: {exc}") from exc


def _filesystem_text_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _path_key(path: Path) -> tuple[str, ...]:
    return tuple(_filesystem_text_key(part) for part in path.parts)


def _same_existing_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


def _paths_overlap(left: Path, right: Path) -> bool:
    left_key = _path_key(left)
    right_key = _path_key(right)
    lexical_overlap = (
        left_key == right_key
        or left_key == right_key[: len(left_key)]
        or right_key == left_key[: len(right_key)]
    )
    if lexical_overlap:
        return True
    return any(
        _same_existing_path(left, candidate)
        for candidate in (right, *right.parents)
    ) or any(
        _same_existing_path(right, candidate)
        for candidate in (left, *left.parents)
    )


def _validate_path_relationships(dest_root: Path, sources: Sequence[Source]) -> None:
    names: dict[str, str] = {}
    for source in sources:
        name_key = _filesystem_text_key(source.name)
        if name_key in names:
            raise RuntimeError(
                f"source names collide on the destination filesystem: "
                f"'{names[name_key]}' and '{source.name}'"
            )
        names[name_key] = source.name
        if _paths_overlap(source.path, dest_root):
            raise RuntimeError(
                f"source '{source.name}' overlaps destination tree: "
                f"{source.path} and {dest_root}"
            )
    for index, source in enumerate(sources):
        for other in sources[index + 1 :]:
            if _paths_overlap(source.path, other.path):
                raise RuntimeError(
                    f"source paths overlap: '{source.name}' ({source.path}) and "
                    f"'{other.name}' ({other.path})"
                )


def _simple_source_name(value: object, index: int) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"source[{index}].name must be a non-empty string")
    if value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
        raise RuntimeError(f"source[{index}].name must be a simple name, not a path")
    return value


def load_config(path: Path) -> Config:
    """Load and strictly validate the TOML configuration."""
    if not path.is_file():
        raise RuntimeError(f"config not found: {path}")
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"cannot read config {path}: {exc}") from exc

    _reject_unknown_keys(raw, {"dest_root", "retention", "source"}, "top-level")
    if "dest_root" not in raw:
        raise RuntimeError("config missing dest_root")
    dest_root = _resolved_config_path(raw["dest_root"], "dest_root")

    retention_raw = raw.get("retention")
    if not isinstance(retention_raw, dict):
        raise RuntimeError("config requires a [retention] table")
    _reject_unknown_keys(
        retention_raw, {"daily", "weekly", "monthly"}, "retention"
    )
    missing_retention = {
        key for key in ("daily", "weekly", "monthly") if key not in retention_raw
    }
    if missing_retention:
        raise RuntimeError(
            f"retention missing key(s): {', '.join(sorted(missing_retention))}"
        )
    retention = Retention(
        daily=_positive_int(retention_raw["daily"], "retention.daily"),
        weekly=_positive_int(retention_raw["weekly"], "retention.weekly"),
        monthly=_positive_int(retention_raw["monthly"], "retention.monthly"),
    )

    source_entries = raw.get("source")
    if not isinstance(source_entries, list) or not source_entries:
        raise RuntimeError("config lists no [[source]] entries")
    sources: list[Source] = []
    names: dict[str, str] = {}
    for index, entry in enumerate(source_entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"source[{index}] must be a table")
        _reject_unknown_keys(entry, {"name", "path", "excludes"}, f"source[{index}]")
        if "name" not in entry or "path" not in entry:
            raise RuntimeError(f"source[{index}] requires name and path")
        name = _simple_source_name(entry["name"], index)
        name_key = _filesystem_text_key(name)
        if name_key in names:
            raise RuntimeError(
                f"duplicate or filesystem-equivalent source name: "
                f"{names[name_key]} and {name}"
            )
        names[name_key] = name
        excludes_raw = entry.get("excludes", [])
        if not isinstance(excludes_raw, list) or not all(
            isinstance(pattern, str) for pattern in excludes_raw
        ):
            raise RuntimeError(f"source[{index}].excludes must be an array of strings")
        sources.append(
            Source(
                name=name,
                path=_resolved_config_path(entry["path"], f"source[{index}].path"),
                excludes=tuple(excludes_raw),
            )
        )

    _validate_path_relationships(dest_root, sources)
    return Config(dest_root=dest_root, retention=retention, sources=tuple(sources))


def _runtime_paths(config: Config, source: Source) -> tuple[Path, Path]:
    """Resolve and recheck one source and its destination immediately before use."""
    try:
        source_path = source.path.resolve()
        dest_root = config.dest_root.resolve()
        all_sources = tuple(
            Source(item.name, item.path.resolve(), item.excludes)
            for item in config.sources
        )
    except OSError as exc:
        raise RuntimeError(f"cannot resolve backup paths: {exc}") from exc
    _validate_path_relationships(dest_root, all_sources)

    dest_candidate = dest_root / source.name
    if dest_candidate.is_symlink():
        raise RuntimeError(
            f"archive destination for '{source.name}' must not be a symlink: "
            f"{dest_candidate}"
        )
    dest_dir = dest_candidate.resolve()
    if dest_root not in dest_dir.parents:
        raise RuntimeError(
            f"archive destination for '{source.name}' escapes dest_root: {dest_dir}"
        )
    if _paths_overlap(source_path, dest_dir):
        raise RuntimeError(
            f"source '{source.name}' overlaps its resolved destination: "
            f"{source_path} and {dest_dir}"
        )
    return source_path, dest_dir


# ------------------------------------------------------------------ logging & alerting


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{now_iso()} {message}", flush=True)


def utc_stamp(moment: dt.datetime | None = None) -> str:
    moment = moment or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("snapshot time must be timezone-aware")
    return moment.astimezone(dt.UTC).strftime(STAMP_FORMAT)


def notify(title: str, message: str) -> None:
    """Post a macOS notification; best effort, never raises."""
    try:
        script = (
            f"display notification {json.dumps(message)} "
            f"with title {json.dumps(title)}"
        )
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:  # noqa: BLE001 - alerting must never crash a backup
        pass


def raise_desktop_flag(text: str) -> bool:
    """Write the persistent alarm, reporting when even that safety net fails."""
    try:
        DESKTOP_FLAG.write_text(text, encoding="utf-8")
    except OSError as exc:
        try:
            print(
                f"{now_iso()} FAIL alarm: could not write {DESKTOP_FLAG}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        except OSError:
            pass
        return False
    return True


def clear_desktop_flag() -> None:
    try:
        DESKTOP_FLAG.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(f"could not clear Desktop backup alarm: {exc}") from exc


# ------------------------------------------------------------------- archive handling


def _excluded(component: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(component, pattern) for pattern in patterns)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_archive_handle(handle: BinaryIO) -> bool:
    try:
        handle.seek(0)
        with gzip.GzipFile(fileobj=handle, mode="rb") as stream:
            while stream.read(1024 * 1024):
                pass
        handle.seek(0)
        with tarfile.open(fileobj=handle, mode="r:gz") as archive:
            return bool(archive.getmembers())
    except (OSError, EOFError, tarfile.TarError, zlib.error):
        return False


def verify_archive(path: Path) -> bool:
    """Fully drain gzip and require a readable, non-empty tar member list."""
    try:
        with path.open("rb") as handle:
            return _verify_archive_handle(handle)
    except OSError:
        return False


def _publish_no_replace(
    directory_fd: int, partial_name: str, final_name: str
) -> None:
    """Atomically publish within one directory without replacing any entry."""
    if sys.platform == "darwin":
        rename = ctypes.CDLL(None, use_errno=True).renameatx_np
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        if rename(
            directory_fd,
            os.fsencode(partial_name),
            directory_fd,
            os.fsencode(final_name),
            0x00000004,  # RENAME_EXCL from <sys/stdio.h>
        ):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), final_name)
        return

    os.link(
        partial_name,
        final_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )
    os.unlink(partial_name, dir_fd=directory_fd)


def _clear_hidden_flag(file_fd: int) -> None:
    """Ensure a dot-prefixed partial becomes visible after publication on macOS."""
    if sys.platform != "darwin":
        return
    hidden_flag = getattr(stat, "UF_HIDDEN", 0)
    if not hidden_flag:
        return
    current_flags = os.fstat(file_fd).st_flags
    if not current_flags & hidden_flag:
        return
    change_flags = ctypes.CDLL(None, use_errno=True).fchflags
    change_flags.argtypes = (ctypes.c_int, ctypes.c_uint)
    change_flags.restype = ctypes.c_int
    if change_flags(file_fd, current_flags & ~hidden_flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def parse_stamp(name: str) -> dt.datetime | None:
    """Parse an exact ``<UTC timestamp>.tar.gz`` filename."""
    if not name.endswith(ARCHIVE_SUFFIX):
        return None
    stamp_text = name[: -len(ARCHIVE_SUFFIX)]
    try:
        parsed = dt.datetime.strptime(stamp_text, STAMP_FORMAT).replace(tzinfo=dt.UTC)
    except ValueError:
        return None
    return parsed if parsed.strftime(STAMP_FORMAT) == stamp_text else None


def create_archive(
    source: Source, dest_dir: Path, stamp: str | None = None
) -> Path:
    """Create, fsync, fully verify, and atomically publish one gzip tar archive."""
    try:
        source_metadata = source.path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"cannot inspect source directory {source.path}: {exc}"
        ) from exc
    if not stat.S_ISDIR(source_metadata.st_mode) or not os.access(
        source.path, os.R_OK | os.X_OK
    ):
        raise RuntimeError(f"source is not a readable directory: {source.path}")
    if stamp is not None and parse_stamp(f"{stamp}{ARCHIVE_SUFFIX}") is None:
        raise ValueError(f"invalid UTC archive stamp: {stamp}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest_dir.is_symlink():
        raise RuntimeError(f"archive destination must not be a symlink: {dest_dir}")
    if stamp is not None:
        requested_final = dest_dir / f"{stamp}{ARCHIVE_SUFFIX}"
        if os.path.lexists(requested_final):
            raise FileExistsError(
                f"completed archive already exists: {requested_final}"
            )
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(dest_dir, directory_flags)
    partial_name: str | None = None
    partial_fd: int | None = None
    published = False
    try:
        partial_stamp = stamp or utc_stamp()
        for _attempt in range(100):
            candidate = (
                f".{partial_stamp}.{secrets.token_hex(8)}.tar.gz.partial"
            )
            try:
                partial_fd = os.open(
                    candidate,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            partial_name = candidate
            break
        else:
            raise FileExistsError(f"could not allocate a partial file in {dest_dir}")

        root_name = source.path.name

        def include(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
            component = member.name.rstrip("/").rsplit("/", 1)[-1]
            if member.name != root_name and _excluded(component, source.excludes):
                return None
            return member

        partial_handle = os.fdopen(partial_fd, "w+b")
        partial_fd = None
        with partial_handle:
            with tarfile.open(fileobj=partial_handle, mode="w:gz") as archive:
                archive.add(source.path, arcname=root_name, filter=include)
            partial_handle.flush()
            os.fsync(partial_handle.fileno())
            if not _verify_archive_handle(partial_handle):
                raise RuntimeError(f"{source.name}: archive failed full verification")

            final_stamp = stamp or utc_stamp()
            final_name = f"{final_stamp}{ARCHIVE_SUFFIX}"
            _publish_no_replace(directory_fd, partial_name, final_name)
            published = True
            _clear_hidden_flag(partial_handle.fileno())
            os.fsync(partial_handle.fileno())
        os.fsync(directory_fd)
        return dest_dir / final_name
    except BaseException as exc:
        if not published and partial_name is not None:
            try:
                os.unlink(partial_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                raise RuntimeError(
                    f"{exc}; also failed to remove partial {partial_name}: "
                    f"{cleanup_error}"
                ) from exc
        raise
    finally:
        if partial_fd is not None:
            os.close(partial_fd)
        os.close(directory_fd)


def _scan_archive_dir(dest_dir: Path) -> tuple[list[Path], list[str]]:
    """Return recognized completed archives and suspicious archive-like names."""
    try:
        destination_metadata = dest_dir.stat()
    except FileNotFoundError:
        return [], []
    if not stat.S_ISDIR(destination_metadata.st_mode):
        raise RuntimeError(f"archive destination is not a directory: {dest_dir}")

    archives: list[Path] = []
    unrecognized: list[str] = []
    for path in dest_dir.iterdir():
        stamp = parse_stamp(path.name)
        try:
            path_metadata = path.lstat()
        except FileNotFoundError:
            continue
        if stamp is not None and stat.S_ISREG(path_metadata.st_mode):
            archives.append(path)
        elif (
            not path.name.startswith(".")
            and not path.name.endswith(".partial")
            and path.name.endswith(".tar.gz")
        ):
            unrecognized.append(path.name)
    archives.sort(
        key=lambda path: parse_stamp(path.name)
        or dt.datetime.min.replace(tzinfo=dt.UTC)
    )
    return archives, sorted(unrecognized)


def list_archives(dest_dir: Path) -> list[Path]:
    """Recognized completed archives, oldest first."""
    archives, _unrecognized = _scan_archive_dir(dest_dir)
    return archives


def archives_by_stamp(dest_dir: Path) -> dict[dt.datetime, Path]:
    return {
        stamp: path
        for path in list_archives(dest_dir)
        if (stamp := parse_stamp(path.name)) is not None
    }


# --------------------------------------------------------------------- GFS retention


def _utc_datetime(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("retention timestamps must be timezone-aware")
    return moment.astimezone(dt.UTC)


def gfs_reasons(
    stamps: Sequence[dt.datetime], retention: Retention
) -> dict[dt.datetime, tuple[str, ...]]:
    """Map retained timestamps to their daily, weekly, and monthly reasons."""
    ordered = sorted(set(stamps), key=_utc_datetime, reverse=True)
    reasons: dict[dt.datetime, set[str]] = {stamp: set() for stamp in ordered}
    policies = (
        ("daily", retention.daily, lambda value: value.date()),
        ("weekly", retention.weekly, lambda value: value.isocalendar()[:2]),
        ("monthly", retention.monthly, lambda value: (value.year, value.month)),
    )
    for reason, count, bucket_for in policies:
        seen: set[object] = set()
        for stamp in ordered:
            bucket = bucket_for(_utc_datetime(stamp))
            if bucket in seen:
                continue
            if len(seen) >= count:
                continue
            seen.add(bucket)
            reasons[stamp].add(reason)

    if ordered and not reasons[ordered[0]]:
        reasons[ordered[0]].add("newest")
    order = {"daily": 0, "weekly": 1, "monthly": 2, "newest": 3}
    return {
        stamp: tuple(sorted(stamp_reasons, key=order.__getitem__))
        for stamp, stamp_reasons in reasons.items()
        if stamp_reasons
    }


def gfs_keep(stamps: Sequence[dt.datetime], retention: Retention) -> set[dt.datetime]:
    return set(gfs_reasons(stamps, retention))


class RetentionCleanupError(RuntimeError):
    """One or more excess archives could not be removed."""

    def __init__(
        self, failures: Sequence[tuple[Path, OSError]], deleted: Sequence[Path]
    ):
        self.failures = tuple(failures)
        self.deleted = tuple(deleted)
        detail = "; ".join(f"{path.name}: {error}" for path, error in failures)
        super().__init__(f"retention cleanup failed: {detail}")


class RetentionSafetyError(RuntimeError):
    """Destructive cleanup was refused because archive time is implausible."""


def prune(
    dest_dir: Path,
    retention: Retention,
    now: dt.datetime | None = None,
) -> list[Path]:
    """Delete only recognized archives outside the GFS keep set."""
    by_stamp = archives_by_stamp(dest_dir)
    now = _utc_datetime(now or dt.datetime.now(dt.UTC))
    future_cutoff = now + dt.timedelta(hours=FUTURE_TOLERANCE_HOURS)
    future = sorted(
        (stamp for stamp in by_stamp if stamp > future_cutoff),
        reverse=True,
    )
    if future:
        names = ", ".join(by_stamp[stamp].name for stamp in future)
        raise RetentionSafetyError(
            "refusing retention cleanup while future-dated archive(s) exist: "
            f"{names}"
        )
    keep = gfs_keep(list(by_stamp), retention)
    deleted: list[Path] = []
    failures: list[tuple[Path, OSError]] = []
    for stamp, path in sorted(by_stamp.items()):
        if stamp in keep:
            continue
        try:
            path.unlink(missing_ok=True)
            deleted.append(path)
        except OSError as exc:
            failures.append((path, exc))
    if deleted:
        try:
            _fsync_dir(dest_dir)
        except OSError as exc:
            failures.append((dest_dir, exc))
    if failures:
        raise RetentionCleanupError(failures, deleted)
    return deleted


# ----------------------------------------------------------------------------- status


@dataclass(frozen=True)
class SnapshotStatus:
    path: Path
    stamp: dt.datetime
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SourceStatus:
    source: Source
    destination: Path
    snapshots: tuple[SnapshotStatus, ...]
    age_hours: float | None
    unrecognized_names: tuple[str, ...]
    problems: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def newest(self) -> SnapshotStatus | None:
        return self.snapshots[0] if self.snapshots else None

    @property
    def archive_count(self) -> int:
        return len(self.snapshots)


def source_status(
    config: Config, source: Source, now: dt.datetime | None = None
) -> SourceStatus:
    """Calculate archive-based status without opening archive contents."""
    now = now or dt.datetime.now(dt.UTC)
    now = _utc_datetime(now)
    destination = config.dest_for(source.name)
    try:
        paths, unrecognized = _scan_archive_dir(destination)
    except (OSError, RuntimeError) as exc:
        return SourceStatus(
            source=source,
            destination=destination,
            snapshots=(),
            age_hours=None,
            unrecognized_names=(),
            problems=(f"cannot read archive directory: {exc}",),
            warnings=(),
        )

    by_stamp = {
        stamp: path
        for path in paths
        if (stamp := parse_stamp(path.name)) is not None
    }
    reasons = gfs_reasons(list(by_stamp), config.retention)
    snapshots = tuple(
        SnapshotStatus(
            path=by_stamp[stamp], stamp=stamp, reasons=reasons.get(stamp, ())
        )
        for stamp in sorted(by_stamp, reverse=True)
    )
    newest = snapshots[0] if snapshots else None
    age_hours = (
        (now - newest.stamp).total_seconds() / 3600 if newest is not None else None
    )
    problems: list[str] = []
    if newest is None:
        problems.append("never backed up")
    elif age_hours is not None and age_hours < -FUTURE_TOLERANCE_HOURS:
        problems.append(
            f"newest snapshot timestamp is {-age_hours:.1f}h in the future"
        )
    elif age_hours is not None and age_hours > STALE_HOURS:
        problems.append(f"no completed snapshot in {age_hours:.0f}h")
    excess = sum(not snapshot.reasons for snapshot in snapshots)
    warnings: list[str] = []
    if excess:
        warnings.append(f"{excess} archive(s) exceed retention")
    if unrecognized:
        warnings.append("unrecognized archive name(s): " + ", ".join(unrecognized))
    return SourceStatus(
        source=source,
        destination=destination,
        snapshots=snapshots,
        age_hours=age_hours,
        unrecognized_names=tuple(unrecognized),
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


def all_statuses(
    config: Config, now: dt.datetime | None = None
) -> tuple[SourceStatus, ...]:
    now = now or dt.datetime.now(dt.UTC)
    return tuple(source_status(config, source, now) for source in config.sources)


def health_problems(config: Config, now: dt.datetime | None = None) -> list[str]:
    return [
        f"{status.source.name}: {problem}"
        for status in all_statuses(config, now)
        for problem in status.problems
    ]


# --------------------------------------------------------------------------- commands


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    deleted: tuple[Path, ...]


def backup_source(config: Config, source: Source) -> BackupResult:
    """Publish one verified full snapshot, then enforce retention."""
    source_path, dest_dir = _runtime_paths(config, source)
    active_source = Source(source.name, source_path, source.excludes)
    archive = create_archive(active_source, dest_dir)
    deleted = prune(dest_dir, config.retention)
    size_mb = archive.stat().st_size / 1_000_000
    log(
        f"OK {source.name}: {archive.name} ({size_mb:.1f} MB); "
        f"pruned {len(deleted)}"
    )
    return BackupResult(archive=archive, deleted=tuple(deleted))


class RunInProgressError(RuntimeError):
    """A healthy existing run already owns the process lock."""


@contextmanager
def run_lock(path: Path | None = None) -> Iterator[None]:
    """Hold the one nonblocking process lock for the complete run."""
    lock_path = path or RUN_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunInProgressError(
                "another icloud_backup run is already in progress"
            ) from exc
        yield
    finally:
        handle.close()


def _run_failure_text(failures: Sequence[tuple[str, str]]) -> str:
    detail = "\n".join(f"- {name}: {error}" for name, error in failures)
    return (
        f"iCloud backup failed at {now_iso()}\n\n{detail}\n\n"
        "Re-run: icloud_backup.py run   "
        "(log: ~/Library/Logs/icloud-backup.log)\n"
    )


def cmd_run(config: Config) -> int:
    """Snapshot every source; clear the alarm only after a completely clean run."""
    with run_lock():
        failures: list[tuple[str, str]] = []
        for source in config.sources:
            try:
                backup_source(config, source)
            except Exception as exc:  # noqa: BLE001 - isolate source failures
                failures.append((source.name, str(exc)))
                log(f"FAIL {source.name}: {exc}")
                notify("iCloud backup FAILED", f"{source.name}: {exc}")
                raise_desktop_flag(_run_failure_text(failures))

        if failures:
            raise_desktop_flag(_run_failure_text(failures))
            return 1
        try:
            clear_desktop_flag()
        except Exception as exc:  # noqa: BLE001 - alarm clearing is run health
            failures.append(("alarm", str(exc)))
            log(f"FAIL alarm: {exc}")
            notify("iCloud backup FAILED", str(exc))
            raise_desktop_flag(_run_failure_text(failures))
            return 1
        return 0


def _format_age(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours < 0:
        future_hours = -age_hours
        if future_hours < 1:
            return f"{future_hours * 60:.0f}m in the future"
        return f"{future_hours:.1f}h in the future"
    if age_hours < 1:
        return f"{age_hours * 60:.0f}m"
    return f"{age_hours:.1f}h"


def cmd_status(config: Config, now: dt.datetime | None = None) -> int:
    """Print archive inventory, freshness, retention reasons, and problems."""
    statuses = all_statuses(config, now)
    for status in statuses:
        print(f"# {status.source.name}  ({status.destination})")
        if status.newest is None:
            print("  newest: none")
        else:
            print(
                f"  newest: {status.newest.path.name}; "
                f"age: {_format_age(status.age_hours)}"
            )
        print(f"  {status.archive_count} archives")
        for snapshot in status.snapshots:
            reason_text = ", ".join(snapshot.reasons) or "prune (excess)"
            print(f"  {snapshot.path.name}\t{reason_text}")
        for problem in status.problems:
            print(f"  WARNING: {problem}")
        for warning in status.warnings:
            print(f"  WARNING: {warning}")
    return 1 if any(status.problems for status in statuses) else 0


# ------------------------------------------------------------------------------- main


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="read configuration from PATH instead of the default",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "run",
        help="create one verified snapshot of every configured source",
        description="Create one verified snapshot of every configured source.",
    )
    subcommands.add_parser(
        "status",
        help="show snapshot freshness and retention reasons without opening archives",
        description=(
            "Show snapshot freshness and retention reasons without opening archives."
        ),
    )
    return parser.parse_args(argv)


def resolve_config_path(explicit: Path | None) -> Path:
    """Config path from ``--config``, then the environment, then the default."""
    if explicit:
        return explicit
    configured = os.environ.get("ICLOUD_BACKUP_CONFIG")
    return Path(configured) if configured else DEFAULT_CONFIG


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(resolve_config_path(args.config))
    if args.command == "status":
        return cmd_status(config)
    try:
        return cmd_run(config)
    except RunInProgressError as exc:
        log(f"SKIP {exc}")
        return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - top-level failures must alarm loudly
        log(f"FATAL {exc}")
        notify("iCloud backup FATAL", str(exc))
        raise_desktop_flag(f"iCloud backup crashed at {now_iso()}\n\n{exc}\n")
        raise SystemExit(1) from exc
