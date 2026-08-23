"""Integration tests for archive contents, integrity, and atomic publication."""

from __future__ import annotations

import datetime as dt
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import icloud_backup as ib  # noqa: E402


STAMP = "20260823T150405Z"


def make_source(root: Path) -> ib.Source:
    source_path = root / "documents"
    (source_path / "nested").mkdir(parents=True)
    (source_path / "empty").mkdir()
    (source_path / "node_modules" / "package").mkdir(parents=True)
    (source_path / "plain.txt").write_text("ordinary file\n", encoding="utf-8")
    (source_path / "nested" / "target.bin").write_bytes(os.urandom(1_250_000))
    (source_path / "nested" / "target-link").symlink_to("target.bin")
    (source_path / ".DS_Store").write_bytes(b"excluded metadata")
    (source_path / "nested" / "cache.pyc").write_bytes(b"excluded bytecode")
    (source_path / "node_modules" / "package" / "index.js").write_text(
        "excluded dependency\n",
        encoding="utf-8",
    )
    return ib.Source(
        name="documents",
        path=source_path,
        excludes=(".DS_Store", "*.pyc", "node_modules"),
    )


class ArchiveRoundTripTests(unittest.TestCase):
    def test_create_verify_extract_round_trip_preserves_tree_and_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            destination = root / "archives"

            archive = ib.create_archive(source, destination, STAMP)

            self.assertEqual(archive, destination / f"{STAMP}.tar.gz")
            self.assertTrue(ib.verify_archive(archive))
            with tarfile.open(archive, "r:gz") as handle:
                members = {member.name: member for member in handle.getmembers()}

            prefix = source.path.name
            self.assertIn(f"{prefix}/plain.txt", members)
            self.assertIn(f"{prefix}/nested/target.bin", members)
            self.assertTrue(members[f"{prefix}/empty"].isdir())
            self.assertTrue(members[f"{prefix}/nested/target-link"].issym())
            self.assertNotIn(f"{prefix}/.DS_Store", members)
            self.assertNotIn(f"{prefix}/nested/cache.pyc", members)
            self.assertFalse(
                any(name.startswith(f"{prefix}/node_modules") for name in members)
            )

            restored = root / "restored"
            restored.mkdir()
            with tarfile.open(archive, "r:gz") as handle:
                handle.extractall(restored, filter="data")
            restored_root = restored / prefix
            self.assertEqual(
                (restored_root / "plain.txt").read_text(encoding="utf-8"),
                "ordinary file\n",
            )
            self.assertEqual(
                (restored_root / "nested" / "target.bin").read_bytes(),
                (source.path / "nested" / "target.bin").read_bytes(),
            )
            self.assertTrue((restored_root / "empty").is_dir())
            self.assertTrue((restored_root / "nested" / "target-link").is_symlink())
            self.assertEqual(
                os.readlink(restored_root / "nested" / "target-link"),
                "target.bin",
            )

    def test_full_stream_verifier_rejects_truncation_corruption_and_empty_tar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            intact = ib.create_archive(source, root / "archives", STAMP)
            intact_bytes = intact.read_bytes()

            truncated = root / "truncated.tar.gz"
            truncated.write_bytes(intact_bytes[:-8])
            corrupted = root / "corrupted.tar.gz"
            corrupt_bytes = bytearray(intact_bytes)
            corrupt_bytes[len(corrupt_bytes) // 2] ^= 0x40
            corrupted.write_bytes(corrupt_bytes)
            empty = root / "empty.tar.gz"
            with tarfile.open(empty, "w:gz"):
                pass

            self.assertTrue(ib.verify_archive(intact))
            self.assertFalse(ib.verify_archive(truncated))
            self.assertFalse(ib.verify_archive(corrupted))
            self.assertFalse(ib.verify_archive(empty))


class PublicationInvariantTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[ib.Config, ib.Source, Path]:
        source = make_source(root)
        destination = root / "backups"
        source_destination = destination / source.name
        source_destination.mkdir(parents=True)
        for offset in range(30):
            moment = dt.datetime(2026, 8, 22, 12, tzinfo=dt.UTC) - dt.timedelta(
                days=offset
            )
            (source_destination / f"{ib.utc_stamp(moment)}.tar.gz").write_bytes(
                b"older completed archive"
            )
        config = ib.Config(
            dest_root=destination,
            retention=ib.Retention(daily=7, weekly=4, monthly=12),
            sources=(source,),
        )
        return config, source, source_destination

    def test_creation_failure_leaves_completed_archives_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, source, destination = self._fixture(Path(tmp))
            completed_before = set(destination.glob("*.tar.gz"))

            with mock.patch.object(
                ib.tarfile,
                "open",
                side_effect=OSError("simulated archive write failure"),
            ):
                with self.assertRaises(OSError):
                    ib.backup_source(config, source)

            self.assertEqual(set(destination.glob("*.tar.gz")), completed_before)
            self.assertEqual(list(destination.glob("*.partial")), [])
            self.assertEqual(list(destination.glob(".*.partial")), [])

    def test_verification_failure_never_publishes_or_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, source, destination = self._fixture(Path(tmp))
            completed_before = set(destination.glob("*.tar.gz"))

            with (
                mock.patch.object(ib, "utc_stamp", return_value=STAMP),
                mock.patch.object(ib, "_verify_archive_handle", return_value=False),
            ):
                with self.assertRaises(RuntimeError):
                    ib.backup_source(config, source)

            self.assertEqual(set(destination.glob("*.tar.gz")), completed_before)
            self.assertFalse((destination / f"{STAMP}.tar.gz").exists())
            self.assertEqual(list(destination.glob("*.partial")), [])
            self.assertEqual(list(destination.glob(".*.partial")), [])

    def test_same_stamp_collision_never_replaces_completed_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            destination = root / "backups" / source.name
            destination.mkdir(parents=True)
            completed = destination / f"{STAMP}.tar.gz"
            completed.write_bytes(b"known completed archive")

            with self.assertRaises(FileExistsError):
                ib.create_archive(source, destination, STAMP)

            self.assertEqual(completed.read_bytes(), b"known completed archive")
            self.assertEqual(list(destination.glob(".*.partial")), [])

    def test_competing_final_at_publication_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            destination = root / "backups" / source.name
            real_publish = ib._publish_no_replace

            def publish_after_competitor(
                directory_fd: int, partial_name: str, final_name: str
            ) -> None:
                competitor_fd = os.open(
                    final_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                with os.fdopen(competitor_fd, "wb") as competitor:
                    competitor.write(b"competing completed archive")
                real_publish(directory_fd, partial_name, final_name)

            with mock.patch.object(
                ib, "_publish_no_replace", side_effect=publish_after_competitor
            ):
                with self.assertRaises(FileExistsError):
                    ib.create_archive(source, destination, STAMP)

            completed = destination / f"{STAMP}.tar.gz"
            self.assertEqual(completed.read_bytes(), b"competing completed archive")
            self.assertEqual(list(destination.glob(".*.partial")), [])

    def test_archive_creation_runs_finder_visibility_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            destination = root / "backups" / source.name

            with mock.patch.object(
                ib,
                "_clear_hidden_flag",
                wraps=ib._clear_hidden_flag,
            ) as clear_hidden:
                archive = ib.create_archive(source, destination, STAMP)

            self.assertTrue(archive.is_file())
            clear_hidden.assert_called_once()

    @unittest.skipUnless(sys.platform == "darwin", "macOS file flags")
    def test_hidden_partial_flag_is_cleared_for_finder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "published.tar.gz"
            path.write_bytes(b"verified archive placeholder")
            os.chflags(path, path.stat().st_flags | stat.UF_HIDDEN)

            with path.open("r+b") as handle:
                ib._clear_hidden_flag(handle.fileno())

            self.assertFalse(path.stat().st_flags & stat.UF_HIDDEN)


if __name__ == "__main__":
    unittest.main()
