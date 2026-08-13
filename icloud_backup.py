#!/usr/bin/env python3
"""Datetime-stamped, compressed, self-contained backups into a local iCloud folder.

Each run writes one independent ``<name>/<UTC-stamp>.tar.<ext>`` per configured
source into ``dest_root`` (a folder inside iCloud Drive). Apple's own sync carries
the archives off-machine — there is no cloud API, token, or credential to maintain.

Design choices that keep it simple and robust:

* **Full, self-contained snapshots.** Every archive stands alone, so pruning is a
  plain file delete and restore is a single extract. There is no delta chain to break.
* **Skip-if-unchanged.** Before archiving, the source is fingerprinted (path + size +
  mtime of every non-excluded file). If nothing changed since the last snapshot, no
  new archive is written — only a heartbeat is updated. Storage and upload churn then
  track how often things actually change, not the schedule.
* **GFS retention.** Keep the newest per day for N days, per week for M weeks, and per
  month for K months. The most recent archive is always kept.
* **Fail loudly.** Any source failing raises a macOS notification and drops a
  ``⚠️ BACKUP FAILING.txt`` on the Desktop; a clean run clears it. A companion
  ``watch.py`` catches the harder case — the job silently not running at all.

Pure standard library. gzip works on any Python 3.11+ (3.11 for ``tomllib``); zstd
needs Python 3.14+.

Commands: ``run`` / ``list`` / ``verify`` / ``restore``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - shareability fallback
    import tomli as tomllib  # type: ignore[no-redef]

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
COMPRESSION = {"gz": ("w:gz", "tar.gz"), "zst": ("w:zst", "tar.zst")}
ARCHIVE_SUFFIXES = (".gz", ".zst")

DEFAULT_CONFIG = Path.home() / ".config/icloud_backup/config.toml"
# STATE_DIR and the Desktop flag can be redirected via env vars (used by tests).
STATE_DIR = Path(os.environ.get("ICLOUD_BACKUP_STATE") or Path.home() / ".local/state/icloud_backup")
DESKTOP_FLAG = Path(os.environ.get("ICLOUD_BACKUP_FLAG") or Path.home() / "Desktop" / "⚠️ BACKUP FAILING.txt")
RUN_LOCK = STATE_DIR / ".run.lock"


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class Retention:
    """How many snapshots to keep in each grandfather-father-son bucket."""

    daily: int = 7
    weekly: int = 4
    monthly: int = 12


@dataclass(frozen=True)
class Source:
    """One backup source: a named folder with optional excludes and retention."""

    name: str
    path: Path
    excludes: tuple[str, ...] = ()
    retention: Retention | None = None


@dataclass(frozen=True)
class Config:
    """Loaded configuration: destination, compression, default retention, sources."""

    dest_root: Path
    compression: str
    stale_hours: float
    retention: Retention
    sources: tuple[Source, ...] = field(default_factory=tuple)

    def dest_for(self, name: str) -> Path:
        """Return the archive directory for the named source."""
        return self.dest_root / name

    def retention_for(self, source: Source) -> Retention:
        """Return the source's own retention, falling back to the global default."""
        return source.retention or self.retention


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve() if p.startswith("~") else Path(p)


def load_config(path: Path) -> Config:
    """Load and validate the TOML config, expanding ``~`` in every path.

    Args:
        path: Path to the TOML config file.

    Returns:
        The parsed configuration.

    Raises:
        RuntimeError: If the file is missing, requests an unsupported compression,
            or defines no ``dest_root`` / no ``[[source]]`` entries.
    """
    if not path.is_file():
        raise RuntimeError(f"config not found: {path}")
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    compression = str(raw.get("compression", "gz"))
    if compression not in COMPRESSION:
        raise RuntimeError(f"compression must be one of {sorted(COMPRESSION)}")
    if compression == "zst" and sys.version_info < (3, 14):
        raise RuntimeError("compression 'zst' needs Python 3.14+; use 'gz'")

    ret_raw = raw.get("retention", {})
    default_retention = Retention(
        daily=int(ret_raw.get("daily", 7)),
        weekly=int(ret_raw.get("weekly", 4)),
        monthly=int(ret_raw.get("monthly", 12)),
    )

    def parse_source(entry: dict[str, object]) -> Source:
        per = entry.get("retention")
        override = (
            Retention(
                daily=int(per.get("daily", default_retention.daily)),
                weekly=int(per.get("weekly", default_retention.weekly)),
                monthly=int(per.get("monthly", default_retention.monthly)),
            )
            if isinstance(per, dict)
            else None
        )
        return Source(
            name=str(entry["name"]),
            path=_expand(str(entry["path"])),
            excludes=tuple(entry.get("excludes", ())),
            retention=override,
        )

    sources = tuple(parse_source(e) for e in raw.get("source", []))
    if not sources:
        raise RuntimeError("config lists no [[source]] entries")

    dest = raw.get("dest_root")
    if not dest:
        raise RuntimeError("config missing dest_root")
    return Config(
        dest_root=_expand(str(dest)),
        compression=compression,
        stale_hours=float(raw.get("stale_hours", 36)),
        retention=default_retention,
        sources=sources,
    )


def selected_sources(config: Config, only: str | None) -> Iterator[Source]:
    """Yield configured sources, optionally filtered to a single name.

    Args:
        config: Loaded configuration.
        only: If given, yield only the source with this name.

    Yields:
        Each matching source, in config order.
    """
    for source in config.sources:
        if only is None or source.name == only:
            yield source


# ------------------------------------------------------------------ logging & alerting


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{now_iso()} {message}", flush=True)


def utc_stamp(moment: dt.datetime | None = None) -> str:
    moment = moment or dt.datetime.now(dt.UTC)
    return moment.astimezone(dt.UTC).strftime(STAMP_FORMAT)


def notify(title: str, message: str) -> None:
    """Post a macOS notification (best effort; never raises)."""
    try:
        script = (
            f"display notification {json.dumps(message)} "
            f"with title {json.dumps(title)}"
        )
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:  # noqa: BLE001 - alerting must never crash a backup
        pass


def raise_desktop_flag(text: str) -> None:
    try:
        DESKTOP_FLAG.write_text(text, encoding="utf-8")
    except OSError:
        pass


def clear_desktop_flag() -> None:
    try:
        DESKTOP_FLAG.unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------------------------- state


def state_path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def read_state(name: str) -> dict[str, object]:
    try:
        return json.loads(state_path(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(name: str, data: dict[str, object]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------- excludes / fingerprint


def _excluded(component: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(component, pat) for pat in patterns)


def iter_entries(root: Path, patterns: Sequence[str]) -> Iterator[tuple[Path, str, bool]]:
    """Walk ``root``, yielding each non-excluded entry once.

    Excluded directories are pruned from the walk (their subtrees are never visited),
    and entries come out in a deterministic order so the fingerprint is stable.

    Args:
        root: Directory to walk.
        patterns: Component-name globs to exclude (e.g. ``node_modules``, ``*.pyc``).

    Yields:
        ``(absolute_path, path_relative_to_root, is_directory)`` for each entry.
    """
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = sorted(d for d in dirnames if not _excluded(d, patterns))
        base = Path(dirpath)
        for name in dirnames:
            p = base / name
            yield p, str(p.relative_to(root)), True
        for name in sorted(filenames):
            if _excluded(name, patterns):
                continue
            p = base / name
            yield p, str(p.relative_to(root)), False


def fingerprint(root: Path, patterns: Sequence[str]) -> str:
    """Compute a cheap change-detecting fingerprint of a directory.

    Hashes the relative path, size, and mtime of every non-excluded file — the same
    heuristic as rsync's default. A change that preserves all three is not detected.

    Args:
        root: Directory to fingerprint.
        patterns: Component-name globs to exclude.

    Returns:
        A hex sha256 digest of the tree's metadata.
    """
    digest = hashlib.sha256()
    for abs_path, rel, is_dir in iter_entries(root, patterns):
        if is_dir:
            continue
        st = abs_path.lstat()
        digest.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()


# ------------------------------------------------------------------- archive handling


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_archive(source: Source, dest_dir: Path, stamp: str, compression: str) -> Path:
    """Write one snapshot archive atomically and return its path.

    The archive is built in a temporary ``.partial`` file, fsynced, then renamed into
    place, so a crash never leaves a half-written archive that looks valid.

    Args:
        source: Source being archived; its ``path`` basename becomes the archive root.
        dest_dir: Directory to write the archive into.
        stamp: UTC timestamp used as the filename.
        compression: ``"gz"`` or ``"zst"``.

    Returns:
        Path to the finished archive.

    Raises:
        Exception: Any error during archiving (the partial file is removed first).
    """
    mode, ext = COMPRESSION[compression]
    dest_dir.mkdir(parents=True, exist_ok=True)
    for leftover in dest_dir.glob("*.partial"):
        leftover.unlink(missing_ok=True)
    final = dest_dir / f"{stamp}.{ext}"
    fd, tmp_name = tempfile.mkstemp(dir=dest_dir, prefix=f".{stamp}.", suffix=".partial")
    os.close(fd)
    tmp = Path(tmp_name)
    root_name = source.path.name
    try:
        with tarfile.open(tmp, mode) as tar:
            tar.add(source.path, arcname=root_name, recursive=False)
            for abs_path, rel, _is_dir in iter_entries(source.path, source.excludes):
                tar.add(abs_path, arcname=f"{root_name}/{rel}", recursive=False)
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        tmp.replace(final)
        _fsync_dir(dest_dir)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final


def verify_archive(path: Path) -> bool:
    """Return True if the archive reopens and holds at least one member.

    Args:
        path: Archive to check.

    Returns:
        True if readable and non-empty, else False.
    """
    try:
        with tarfile.open(path, "r:*") as tar:
            return any(True for _ in tar)
    except (tarfile.TarError, OSError):
        return False


def parse_stamp(name: str) -> dt.datetime | None:
    """Parse the UTC timestamp from an archive filename, or None if it doesn't match."""
    base = name.split(".", 1)[0]
    try:
        return dt.datetime.strptime(base, STAMP_FORMAT).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def list_archives(dest_dir: Path) -> list[Path]:
    """Return archive files in ``dest_dir``, oldest first (by timestamped name)."""
    if not dest_dir.is_dir():
        return []
    found = [
        p
        for p in dest_dir.iterdir()
        if p.suffix in ARCHIVE_SUFFIXES and parse_stamp(p.name) is not None
    ]
    return sorted(found, key=lambda p: p.name)


def archives_by_stamp(dest_dir: Path) -> dict[dt.datetime, Path]:
    """Map each archive's parsed UTC timestamp to its path (unparseable names skipped)."""
    return {
        stamp: path
        for path in list_archives(dest_dir)
        if (stamp := parse_stamp(path.name)) is not None
    }


# --------------------------------------------------------------------- GFS retention


def gfs_keep(stamps: Sequence[dt.datetime], retention: Retention) -> set[dt.datetime]:
    """Select which timestamps to keep under grandfather-father-son retention.

    Keeps the newest snapshot in each of the most recent ``daily`` days, ``weekly``
    ISO weeks, and ``monthly`` months; the single most recent snapshot is always kept.

    Args:
        stamps: All snapshot timestamps for one source.
        retention: How many daily/weekly/monthly buckets to keep.

    Returns:
        The subset of ``stamps`` to retain; everything else may be pruned.
    """
    if not stamps:
        return set()
    ordered = sorted(stamps, reverse=True)
    keep: set[dt.datetime] = {ordered[0]}  # always keep the most recent
    buckets = (
        (retention.daily, "%Y%m%d"),
        (retention.weekly, "%G%V"),
        (retention.monthly, "%Y%m"),
    )
    for count, key_format in buckets:
        if count <= 0:
            continue
        seen: set[str] = set()
        for stamp in ordered:
            key = stamp.strftime(key_format)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) <= count:
                keep.add(stamp)
            else:
                break  # remaining stamps are all in older, over-quota buckets
    return keep


def prune(dest_dir: Path, retention: Retention) -> list[Path]:
    """Delete archives outside the GFS keep-set.

    Args:
        dest_dir: A source's archive directory.
        retention: Retention policy to apply.

    Returns:
        The archive paths that were deleted.
    """
    by_stamp = archives_by_stamp(dest_dir)
    keep = gfs_keep(list(by_stamp), retention)
    deleted = []
    for stamp, path in by_stamp.items():
        if stamp not in keep:
            path.unlink(missing_ok=True)
            deleted.append(path)
    return deleted


# --------------------------------------------------------------------------- commands


def backup_source(config: Config, source: Source) -> str:
    """Back up one source: skip if unchanged, else archive, verify, prune, and record.

    Args:
        config: Loaded configuration.
        source: The source to back up.

    Returns:
        A one-line, human-readable summary of what happened.

    Raises:
        RuntimeError: If the source path is missing or the new archive fails verification.
    """
    if not source.path.is_dir():
        raise RuntimeError(f"source path not found: {source.path}")
    dest_dir = config.dest_for(source.name)
    fp = fingerprint(source.path, source.excludes)
    state = read_state(source.name)

    if state.get("fingerprint") == fp and list_archives(dest_dir):
        state.update(last_run=now_iso(), last_status="ok-unchanged")
        write_state(source.name, state)
        log(f"OK {source.name}: unchanged, skipped")
        return f"{source.name}: unchanged"

    stamp = utc_stamp()
    archive = create_archive(source, dest_dir, stamp, config.compression)
    if not verify_archive(archive):
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"{source.name}: archive failed verification; removed")
    deleted = prune(dest_dir, config.retention_for(source))
    size_mb = archive.stat().st_size / 1_000_000
    state.update(
        last_run=now_iso(),
        last_status="ok-archived",
        last_archive=archive.name,
        fingerprint=fp,
    )
    write_state(source.name, state)
    log(f"OK {source.name}: {archive.name} ({size_mb:.1f} MB); pruned {len(deleted)}")
    return f"{source.name}: {archive.name} ({size_mb:.1f} MB)"


@contextmanager
def run_lock() -> Iterator[None]:
    """Serialize runs; exit rather than let a manual run overlap the scheduled one."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(RUN_LOCK, "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another icloud_backup run is already in progress")
        yield
    finally:
        handle.close()


def cmd_run(config: Config, only: str | None) -> int:
    """Back up every selected source, alarming loudly if any fails."""
    with run_lock():
        failures: list[tuple[str, str]] = []
        for source in selected_sources(config, only):
            try:
                backup_source(config, source)
            except Exception as exc:  # noqa: BLE001 - isolate one source's failure
                failures.append((source.name, str(exc)))
                log(f"FAIL {source.name}: {exc}")
                state = read_state(source.name)
                state.update(last_run=now_iso(), last_status="error", last_error=str(exc))
                write_state(source.name, state)

        if failures:
            detail = "\n".join(f"- {name}: {err}" for name, err in failures)
            notify("iCloud backup FAILED", "; ".join(n for n, _ in failures))
            raise_desktop_flag(
                f"iCloud backup failed at {now_iso()}\n\n{detail}\n\n"
                "Re-run: icloud_backup.py run   (log: ~/Library/Logs/icloud-backup.log)\n"
            )
            return 1
        clear_desktop_flag()
        return 0


def cmd_list(config: Config, only: str | None) -> int:
    """List archives per source, marking which the next prune would keep or drop."""
    for source in selected_sources(config, only):
        by_stamp = archives_by_stamp(config.dest_for(source.name))
        keep = gfs_keep(list(by_stamp), config.retention_for(source))
        total = 0
        print(f"# {source.name}  ({config.dest_for(source.name)})")
        for stamp, path in sorted(by_stamp.items()):
            size = path.stat().st_size
            total += size
            mark = "keep" if stamp in keep else "prune"
            print(f"  {path.name}\t{size / 1_000_000:8.1f} MB  {mark}")
        print(f"  -> {len(by_stamp)} archives, {total / 1_000_000:.1f} MB")
    return 0


def cmd_verify(config: Config, only: str | None) -> int:
    """Reopen every archive and report readability; nonzero exit if any is bad."""
    bad = 0
    for source in selected_sources(config, only):
        for path in list_archives(config.dest_for(source.name)):
            ok = verify_archive(path)
            print(f"{'OK  ' if ok else 'BAD '}{source.name}/{path.name}")
            bad += 0 if ok else 1
    return 0 if bad == 0 else 1


def cmd_restore(config: Config, name: str, at: str | None, target: str | None) -> int:
    """Extract one source's snapshot (newest, or matching ``at``) to ``target``."""
    archives = list_archives(config.dest_for(name))
    if not archives:
        raise RuntimeError(f"no archives for source '{name}'")
    chosen = (
        archives[-1]
        if not at
        else next((p for p in reversed(archives) if p.name.startswith(at)), None)
    )
    if chosen is None:
        raise RuntimeError(f"no archive matching '{at}' for '{name}'")
    out = (
        Path(target)
        if target
        else Path.home() / "Desktop" / f"restore-{name}-{chosen.name.split('.')[0]}"
    )
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(chosen, "r:*") as tar:
        tar.extractall(out, filter="data")
    print(f"restored {name}/{chosen.name} -> {out}")
    return 0


# ------------------------------------------------------------------------------- main


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ("run", "list", "verify"):
        p = sub.add_parser(cmd)
        p.add_argument("--only", default=None, help="limit to one source by name")
    r = sub.add_parser("restore")
    r.add_argument("name")
    r.add_argument("--at", default=None, help="archive stamp prefix (default: newest)")
    r.add_argument("--target", default=None, help="output dir (default: ~/Desktop/restore-…)")
    return parser.parse_args(argv)


def resolve_config_path(explicit: Path | None) -> Path:
    """Resolve the config path from ``--config``, then ``$ICLOUD_BACKUP_CONFIG``, then default."""
    if explicit:
        return explicit
    env = os.environ.get("ICLOUD_BACKUP_CONFIG")
    return Path(env) if env else DEFAULT_CONFIG


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("icloud_backup needs Python 3.11+", file=sys.stderr)
        return 2
    args = parse_args(argv)
    config = load_config(resolve_config_path(args.config))
    dispatch = {
        "run": lambda: cmd_run(config, args.only),
        "list": lambda: cmd_list(config, args.only),
        "verify": lambda: cmd_verify(config, args.only),
        "restore": lambda: cmd_restore(config, args.name, args.at, args.target),
    }
    return dispatch[args.command]()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - top-level guard; also alert loudly
        log(f"FATAL {exc}")
        notify("iCloud backup FATAL", str(exc))
        raise_desktop_flag(f"iCloud backup crashed at {now_iso()}\n\n{exc}\n")
        raise SystemExit(1) from exc
