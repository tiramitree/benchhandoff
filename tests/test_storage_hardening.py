from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.storage as storage
from benchhandoff.errors import BoundaryError, EvidenceError
from tests.workspace_temp import WorkspaceTemporaryDirectory


class StorageHardeningTests(unittest.TestCase):
    def temporary_root(self) -> WorkspaceTemporaryDirectory:
        return WorkspaceTemporaryDirectory(prefix="benchhandoff-storage-")

    def test_windows_forbidden_characters_and_c0_controls_are_rejected(self) -> None:
        invalid = (
            "bad<name",
            "bad>name",
            'bad"name',
            "bad|name",
            "bad?name",
            "bad*name",
            "bad:name",
            "bad/name",
            "bad\\name",
            "bad\x00name",
            "bad\x01name",
            "bad\x1fname",
        )
        for value in invalid:
            with self.subTest(value=ascii(value)), self.assertRaises(BoundaryError):
                storage.windows_component_key(value, label="component")

    def test_windows_extended_device_names_are_rejected_case_insensitively(self) -> None:
        invalid = (
            "CONIN$",
            "conout$.txt",
            "Clock$",
            "COM\u00b9",
            "com\u00b2.log",
            "CoM\u00b3.data",
            "LPT\u00b9",
            "lpt\u00b2.txt",
            "LpT\u00b3.log",
        )
        for value in invalid:
            with self.subTest(value=ascii(value)), self.assertRaises(BoundaryError):
                storage.windows_component_key(value, label="component")

        for value in ("COM\u2074.txt", "LPT0", "CONIN-dollar", "clock.txt"):
            with self.subTest(value=ascii(value)):
                self.assertEqual(
                    storage.windows_component_key(value, label="component"),
                    value.casefold(),
                )

    def test_windows_component_utf8_limit_is_conservative_and_byte_based(self) -> None:
        self.assertEqual(
            storage.windows_component_key("a" * 240, label="component"),
            "a" * 240,
        )
        self.assertEqual(
            storage.windows_component_key("\u754c" * 80, label="component"),
            "\u754c" * 80,
        )
        for value in ("a" * 241, "\u754c" * 81):
            with self.subTest(length=len(value)), self.assertRaises(BoundaryError):
                storage.windows_component_key(value, label="component")

    def test_normalized_path_applies_windows_component_rules(self) -> None:
        for value in ("dir/bad?.txt", "dir/COM\u00b9.txt", f"dir/{'a' * 241}"):
            with self.subTest(value=ascii(value)), self.assertRaises(BoundaryError):
                storage.normalize_relative_file(value, label="path")

    def test_portable_path_total_utf8_limit_is_bounded(self) -> None:
        allowed = "/".join(["a" * 200] * 5)
        rejected = "/".join(["a" * 200] * 6)
        self.assertEqual(
            storage.normalize_relative_file(allowed, label="path"),
            allowed,
        )
        with self.assertRaisesRegex(BoundaryError, "UTF-8 path limit"):
            storage.normalize_relative_file(rejected, label="path")

    def test_atomic_write_json_refuses_output_its_reader_cannot_accept(self) -> None:
        with self.temporary_root() as temporary:
            destination = Path(temporary) / "oversized.json"
            with mock.patch.object(storage, "_MAX_JSON_BYTES", 8):
                with self.assertRaisesRegex(EvidenceError, "reader limit"):
                    storage.atomic_write_json(destination, {"payload": "too large"})
            self.assertFalse(destination.exists())

    def test_atomic_writer_rejects_depth_its_reader_cannot_accept(self) -> None:
        with self.temporary_root() as temporary:
            destination = Path(temporary) / "too-deep.json"
            value: object = 0
            for _ in range(65):
                value = [value]
            with self.assertRaisesRegex(EvidenceError, "maximum JSON depth of 64"):
                storage.atomic_write_json(destination, value)
            self.assertFalse(destination.exists())

    def test_atomic_writer_rejects_nodes_its_reader_cannot_accept(self) -> None:
        with self.temporary_root() as temporary:
            destination = Path(temporary) / "too-many-nodes.json"
            with self.assertRaisesRegex(
                EvidenceError,
                "maximum JSON node count of 100000",
            ):
                storage.atomic_write_json(destination, [0] * 100_000)
            self.assertFalse(destination.exists())

    def test_atomic_writer_rejects_nonfinite_numbers(self) -> None:
        with self.temporary_root() as temporary:
            destination = Path(temporary) / "nonfinite.json"
            for value in (float("nan"), float("inf"), float("-inf")):
                with (
                    self.subTest(value=repr(value)),
                    self.assertRaisesRegex(EvidenceError, "non-finite JSON number"),
                ):
                    storage.atomic_write_json(destination, {"value": value})
                self.assertFalse(destination.exists())

    def test_json_reader_rejects_exponent_overflow_to_infinity(self) -> None:
        with self.temporary_root() as temporary:
            path = Path(temporary) / "overflow.json"
            path.write_text('{"value":1e9999}', encoding="utf-8")
            with self.assertRaisesRegex(EvidenceError, "non-finite JSON number"):
                storage.read_json_file(path, label="overflow")

    def test_atomic_write_bytes_replaces_exact_payload_without_temp_residue(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            destination = root / "record.bin"
            storage.atomic_write_bytes(destination, b"first")
            self.assertEqual(destination.read_bytes(), b"first")
            storage.atomic_write_bytes(destination, b"second\x00payload")
            self.assertEqual(destination.read_bytes(), b"second\x00payload")
            self.assertEqual(
                [path for path in root.iterdir() if path.name.endswith(".tmp")],
                [],
            )

    def test_atomic_write_json_delegates_canonical_bytes(self) -> None:
        destination = Path("unused.json")
        value = {"b": 2, "a": 1}
        with mock.patch.object(storage, "atomic_write_bytes") as write_bytes:
            storage.atomic_write_json(destination, value)
        write_bytes.assert_called_once_with(
            destination,
            storage.canonical_json_bytes(value),
        )

    def test_read_regular_bytes_enforces_exact_limit_and_keeps_default_compatible(self) -> None:
        with self.temporary_root() as temporary:
            path = Path(temporary) / "payload.bin"
            path.write_bytes(b"12345")
            self.assertEqual(
                storage.read_regular_bytes(path, label="payload"),
                b"12345",
            )
            self.assertEqual(
                storage.read_regular_bytes(path, label="payload", max_bytes=5),
                b"12345",
            )
            with self.assertRaisesRegex(EvidenceError, "4-byte size limit"):
                storage.read_regular_bytes(path, label="payload", max_bytes=4)
            for invalid in (-1, True, 1.5):
                with self.subTest(max_bytes=invalid), self.assertRaises(ValueError):
                    storage.read_regular_bytes(
                        path,
                        label="payload",
                        max_bytes=invalid,  # type: ignore[arg-type]
                    )

    def test_read_json_file_uses_sixteen_mib_limit(self) -> None:
        with mock.patch.object(
            storage,
            "read_regular_bytes",
            return_value=b"{}",
        ) as read_bytes:
            self.assertEqual(storage.read_json_file("unused", label="evidence"), {})
        read_bytes.assert_called_once_with(
            "unused",
            label="evidence",
            max_bytes=16 * 1024 * 1024,
        )

    def test_json_depth_boundary_is_iterative_and_exact(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            accepted = root / "depth-64.json"
            rejected = root / "depth-65.json"
            accepted.write_bytes(b"[" * 64 + b"0" + b"]" * 64)
            rejected.write_bytes(b"[" * 65 + b"0" + b"]" * 65)
            storage.read_json_file(accepted, label="depth-64")
            with self.assertRaisesRegex(EvidenceError, "maximum JSON depth of 64"):
                storage.read_json_file(rejected, label="depth-65")

    def test_json_node_boundary_counts_container_and_values(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            accepted = root / "nodes-100000.json"
            rejected = root / "nodes-100001.json"
            accepted.write_text(json.dumps([0] * 99_999), encoding="utf-8")
            rejected.write_text(json.dumps([0] * 100_000), encoding="utf-8")
            self.assertEqual(
                len(storage.read_json_file(accepted, label="nodes-100000")),
                99_999,
            )
            with self.assertRaisesRegex(
                EvidenceError,
                "maximum JSON node count of 100000",
            ):
                storage.read_json_file(rejected, label="nodes-100001")

    def test_json_parser_recursion_and_overflow_are_stable_evidence_errors(self) -> None:
        for failure in (
            RecursionError("synthetic recursion"),
            OverflowError("synthetic overflow"),
        ):
            with (
                self.subTest(error=type(failure).__name__),
                mock.patch.object(
                    storage,
                    "read_regular_bytes",
                    return_value=b"{}",
                ),
                mock.patch.object(storage.json, "loads", side_effect=failure),
            ):
                with self.assertRaisesRegex(EvidenceError, "safe JSON parser limits"):
                    storage.read_json_file("unused", label="evidence")

    def test_move_regular_same_filesystem_moves_and_rechecks_identity(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            source_directory = root / "source"
            destination_directory = root / "destination"
            source_directory.mkdir()
            destination_directory.mkdir()
            source = source_directory / "partial.bin"
            destination = destination_directory / "artifact.bin"
            source.write_bytes(b"partial-output")
            expected = storage.file_identity(source, label="source")
            with mock.patch.object(storage, "_fsync_directory") as fsync_directory:
                actual = storage.move_regular_same_filesystem(
                    source,
                    destination,
                    label="quarantine artifact",
                )
            self.assertEqual(actual, expected)
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"partial-output")
            if os.name == "nt":
                fsync_directory.assert_not_called()
            else:
                self.assertEqual(
                    fsync_directory.call_args_list,
                    [mock.call(source_directory), mock.call(destination_directory)],
                )

    def test_move_regular_same_filesystem_rejects_existing_destination(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            destination = root / "destination.bin"
            source.write_bytes(b"source")
            destination.write_bytes(b"destination")
            with self.assertRaises(BoundaryError):
                storage.move_regular_same_filesystem(
                    source,
                    destination,
                    label="quarantine artifact",
                )
            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(destination.read_bytes(), b"destination")

    def test_move_regular_same_filesystem_rejects_device_mismatch(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            source_directory = root / "source"
            destination_directory = root / "destination"
            source_directory.mkdir()
            destination_directory.mkdir()
            source = source_directory / "source.bin"
            destination = destination_directory / "destination.bin"
            source.write_bytes(b"source")
            source_device = source.lstat().st_dev
            real_stat = Path.stat

            def mismatched_destination(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> object:
                result = real_stat(path, *args, **kwargs)
                if Path(path) == destination_directory:
                    return SimpleNamespace(st_dev=source_device + 1, st_mode=result.st_mode)
                return result

            with mock.patch.object(
                Path,
                "stat",
                autospec=True,
                side_effect=mismatched_destination,
            ):
                with self.assertRaisesRegex(BoundaryError, "same filesystem"):
                    storage.move_regular_same_filesystem(
                        source,
                        destination,
                        label="quarantine artifact",
                    )
            self.assertTrue(source.is_file())
            self.assertFalse(destination.exists())

    def test_move_regular_same_filesystem_rejects_post_move_identity_drift(self) -> None:
        with self.temporary_root() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            destination = root / "destination.bin"
            source.write_bytes(b"source")
            expected = {"sha256": "a" * 64, "size": 6}
            changed = {"sha256": "b" * 64, "size": 6}
            with mock.patch.object(
                storage,
                "file_identity",
                side_effect=(expected, changed),
            ):
                with self.assertRaisesRegex(EvidenceError, "identity changed"):
                    storage.move_regular_same_filesystem(
                        source,
                        destination,
                        label="quarantine artifact",
                    )


if __name__ == "__main__":
    unittest.main()
