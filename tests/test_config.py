"""Strict configuration and resolved-path safety tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import icloud_backup as ib  # noqa: E402


def quoted(path: str | Path) -> str:
    return json.dumps(str(path))


def render_config(
    destination: str | Path,
    sources: list[tuple[str, str | Path]],
    *,
    root_extra: str = "",
    retention: tuple[str, str, str] = ("7", "4", "12"),
    retention_extra: str = "",
    source_extra: str = "",
) -> str:
    daily, weekly, monthly = retention
    lines = [f"dest_root = {quoted(destination)}"]
    if root_extra:
        lines.append(root_extra)
    lines.extend(
        [
            "",
            "[retention]",
            f"daily = {daily}",
            f"weekly = {weekly}",
            f"monthly = {monthly}",
        ]
    )
    if retention_extra:
        lines.append(retention_extra)
    for name, path in sources:
        lines.extend(
            [
                "",
                "[[source]]",
                f"name = {json.dumps(name)}",
                f"path = {quoted(path)}",
                'excludes = [".DS_Store", "*.pyc"]',
            ]
        )
        if source_extra:
            lines.append(source_extra)
    return "\n".join(lines) + "\n"


def load_text(root: Path, text: str) -> ib.Config:
    config_path = root / "config.toml"
    config_path.write_text(text, encoding="utf-8")
    return ib.load_config(config_path)


class ConfigSafetyTests(unittest.TestCase):
    def test_tilde_paths_expand_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "documents").mkdir()
            config_text = render_config("~/backups", [("documents", "~/documents")])

            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                config = load_text(home, config_text)

            self.assertEqual(config.dest_root, (home / "backups").resolve())
            self.assertEqual(config.sources[0].path, (home / "documents").resolve())
            self.assertEqual(config.retention, ib.Retention(7, 4, 12))

    def test_rejects_relative_paths_names_and_nonpositive_or_noninteger_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "backups"
            source_a = root / "source-a"
            source_b = root / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            cases = {
                "relative destination": render_config(
                    "backups",
                    [("source-a", source_a)],
                ),
                "relative source": render_config(
                    destination,
                    [("source-a", "source-a")],
                ),
                "duplicate names": render_config(
                    destination,
                    [("same", source_a), ("same", source_b)],
                ),
                "case-equivalent names": render_config(
                    destination,
                    [("Source", source_a), ("source", source_b)],
                ),
                "unicode-equivalent names": render_config(
                    destination,
                    [("caf\N{LATIN SMALL LETTER E WITH ACUTE}", source_a),
                     ("cafe\N{COMBINING ACUTE ACCENT}", source_b)],
                ),
                "blank name": render_config(destination, [("", source_a)]),
                "dot name": render_config(destination, [(".", source_a)]),
                "dot-dot name": render_config(destination, [("..", source_a)]),
                "path-like name": render_config(
                    destination,
                    [("nested/name", source_a)],
                ),
                "zero daily": render_config(
                    destination,
                    [("source-a", source_a)],
                    retention=("0", "4", "12"),
                ),
                "negative weekly": render_config(
                    destination,
                    [("source-a", source_a)],
                    retention=("7", "-1", "12"),
                ),
                "boolean monthly": render_config(
                    destination,
                    [("source-a", source_a)],
                    retention=("7", "4", "true"),
                ),
                "float daily": render_config(
                    destination,
                    [("source-a", source_a)],
                    retention=("7.0", "4", "12"),
                ),
                "string weekly": render_config(
                    destination,
                    [("source-a", source_a)],
                    retention=("7", '"4"', "12"),
                ),
            }

            for label, config_text in cases.items():
                with self.subTest(label):
                    with self.assertRaises(RuntimeError):
                        load_text(root, config_text)

    def test_rejects_unknown_and_removed_keys_at_every_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "backups"
            source = root / "source"
            source.mkdir()
            cases = {
                "unknown root": render_config(
                    destination,
                    [("source", source)],
                    root_extra="surprise = true",
                ),
                "removed compression": render_config(
                    destination,
                    [("source", source)],
                    root_extra='compression = "gz"',
                ),
                "removed stale hours": render_config(
                    destination,
                    [("source", source)],
                    root_extra="stale_hours = 36",
                ),
                "unknown retention": render_config(
                    destination,
                    [("source", source)],
                    retention_extra="yearly = 2",
                ),
                "unknown source": render_config(
                    destination,
                    [("source", source)],
                    source_extra="mystery = true",
                ),
                "removed source retention": render_config(
                    destination,
                    [("source", source)],
                    source_extra="retention = { daily = 2 }",
                ),
            }

            for label, config_text in cases.items():
                with self.subTest(label):
                    with self.assertRaises(RuntimeError):
                        load_text(root, config_text)

    def test_rejects_resolved_source_destination_and_source_source_overlaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            nested = source / "nested"
            nested.mkdir(parents=True)
            source_alias = root / "source-alias"
            source_alias.symlink_to(source, target_is_directory=True)
            cases = {
                "destination inside source": render_config(
                    source / "backups",
                    [("source", source)],
                ),
                "source inside destination": render_config(
                    source,
                    [("nested", nested)],
                ),
                "same source and destination": render_config(
                    source,
                    [("source", source)],
                ),
                "nested sources": render_config(
                    root / "backups",
                    [("source", source), ("nested", nested)],
                ),
                "case-equivalent source paths": render_config(
                    root / "backups",
                    [("first", source), ("second", root / "Source")],
                ),
                "symlink-resolved overlap": render_config(
                    source / "backups",
                    [("source", source_alias)],
                ),
            }

            for label, config_text in cases.items():
                with self.subTest(label):
                    with self.assertRaises(RuntimeError):
                        load_text(root, config_text)

    def test_runtime_rejects_symlinked_per_source_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "source"
            source_path.mkdir()
            destination = root / "backups"
            destination.mkdir()
            escape = root / "escape"
            escape.mkdir()
            (destination / "source").symlink_to(escape, target_is_directory=True)
            source = ib.Source("source", source_path)
            config = ib.Config(destination, ib.Retention(7, 4, 12), (source,))

            with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
                ib.backup_source(config, source)

            self.assertEqual(list(escape.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
