from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPOSITORY_ROOT / "tools" / "verify_license_state.py"
FINALIZE_SCRIPT = REPOSITORY_ROOT / "tools" / "finalize_license.py"


def _load(name: str, path: Path) -> object:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


VERIFY = _load("benchhandoff_license_state", VERIFY_SCRIPT)
TOOLS_PATH = str(VERIFY_SCRIPT.parent)
if TOOLS_PATH not in sys.path:
    sys.path.insert(0, TOOLS_PATH)
FINALIZE = _load("benchhandoff_finalize_license", FINALIZE_SCRIPT)

PENDING = (
    "# License Pending\n\n"
    "No open-source license has been selected for this fixture.\n"
).encode()
LICENSE_BYTES = b"fixture canonical license\n"
LICENSE_SHA = hashlib.sha256(LICENSE_BYTES).hexdigest()
PYPROJECT_PENDING = """\
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "benchhandoff"
version = "0.1.0"
readme = "README.md"
""".encode()


def _specs() -> dict[str, object]:
    return {
        "MIT": VERIFY.LicenseSpec(
            identifier="MIT",
            size=len(LICENSE_BYTES),
            sha256=LICENSE_SHA,
            source_commit="a" * 40,
            source_url="https://example.com/MIT.txt",
        )
    }


class LicenseStateTests(unittest.TestCase):
    def _repository(self, root: Path) -> Path:
        repository = root / "repository"
        repository.mkdir()
        (repository / "pyproject.toml").write_bytes(PYPROJECT_PENDING)
        (repository / "LICENSING_STATUS.md").write_bytes(PENDING)
        return repository

    def test_checked_in_license_state_passes(self) -> None:
        state = VERIFY.inspect_license_state(REPOSITORY_ROOT)
        self.assertIn(state["mode"], {"pending", "final"})
        self.assertNotEqual(
            (REPOSITORY_ROOT / "LICENSE").exists(),
            (REPOSITORY_ROOT / "LICENSING_STATUS.md").exists(),
        )

    def test_cli_emits_canonical_pending_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    VERIFY.main(["--repository", str(repository)]),
                    0,
                )
            parsed = json.loads(stdout.getvalue())
            self.assertEqual(parsed["mode"], "pending")
            self.assertEqual(
                stdout.getvalue(),
                json.dumps(parsed, sort_keys=True, separators=(",", ":")) + "\n",
            )

    def test_cli_require_final_rejects_pending_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = VERIFY.main(
                    ["--repository", str(repository), "--require-final"]
                )
            self.assertEqual(code, 1)
            self.assertIn("final license state is required", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_ambiguous_and_missing_states_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            (repository / "LICENSE").write_bytes(LICENSE_BYTES)
            with self.assertRaisesRegex(
                VERIFY.LicenseStateError,
                "exactly one",
            ):
                VERIFY.inspect_license_state(repository, license_specs=_specs())
            (repository / "LICENSING_STATUS.md").unlink()
            (repository / "LICENSE").unlink()
            with self.assertRaisesRegex(
                VERIFY.LicenseStateError,
                "exactly one",
            ):
                VERIFY.inspect_license_state(repository, license_specs=_specs())

    def test_final_state_requires_matching_metadata_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            (repository / "LICENSING_STATUS.md").unlink()
            (repository / "LICENSE").write_bytes(LICENSE_BYTES)
            final = PYPROJECT_PENDING.decode().replace(
                'requires = ["setuptools>=69"]',
                'requires = ["setuptools>=77.0.3"]',
            ).replace(
                'readme = "README.md"',
                'readme = "README.md"\nlicense = "MIT"\nlicense-files = ["LICENSE"]',
            )
            (repository / "pyproject.toml").write_text(
                final,
                encoding="utf-8",
                newline="\n",
            )
            state = VERIFY.inspect_license_state(
                repository,
                license_specs=_specs(),
            )
            self.assertEqual(state["mode"], "final")
            self.assertEqual(state["license_sha256"], LICENSE_SHA)
            (repository / "LICENSE").write_bytes(LICENSE_BYTES + b"x\n")
            with self.assertRaisesRegex(
                VERIFY.LicenseStateError,
                "does not match",
            ):
                VERIFY.inspect_license_state(repository, license_specs=_specs())

    def test_final_state_rejects_stale_build_backend_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            (repository / "LICENSING_STATUS.md").unlink()
            (repository / "LICENSE").write_bytes(LICENSE_BYTES)
            final = PYPROJECT_PENDING.decode().replace(
                'readme = "README.md"',
                'readme = "README.md"\nlicense = "MIT"\nlicense-files = ["LICENSE"]',
            )
            (repository / "pyproject.toml").write_text(
                final,
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaisesRegex(
                VERIFY.LicenseStateError,
                "setuptools>=77.0.3",
            ):
                VERIFY.inspect_license_state(repository, license_specs=_specs())

    def test_finalizer_dry_run_is_deterministic_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            license_file = root / "candidate.txt"
            license_file.write_bytes(LICENSE_BYTES)
            before = {
                path.name: path.read_bytes()
                for path in repository.iterdir()
            }
            first = FINALIZE.finalize_repository(
                repository,
                choice="MIT",
                license_file=license_file,
                expected_source_commit=None,
                apply=False,
                require_clean_git=False,
                license_specs=_specs(),
            )
            second = FINALIZE.finalize_repository(
                repository,
                choice="MIT",
                license_file=license_file,
                expected_source_commit=None,
                apply=False,
                require_clean_git=False,
                license_specs=_specs(),
            )
            self.assertEqual(first, second)
            self.assertFalse(first["applied"])
            self.assertEqual(
                before,
                {path.name: path.read_bytes() for path in repository.iterdir()},
            )

    def test_finalizer_applies_and_verifies_exact_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            license_file = root / "candidate.txt"
            license_file.write_bytes(LICENSE_BYTES)
            result = FINALIZE.finalize_repository(
                repository,
                choice="MIT",
                license_file=license_file,
                expected_source_commit=None,
                apply=True,
                require_clean_git=False,
                license_specs=_specs(),
            )
            self.assertTrue(result["applied"])
            self.assertEqual(result["verified_state"]["mode"], "final")
            self.assertEqual((repository / "LICENSE").read_bytes(), LICENSE_BYTES)
            self.assertFalse((repository / "LICENSING_STATUS.md").exists())
            self.assertIn(
                'license-files = ["LICENSE"]',
                (repository / "pyproject.toml").read_text(encoding="utf-8"),
            )

    def test_post_transition_failure_rolls_back_all_three_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            license_file = root / "candidate.txt"
            license_file.write_bytes(LICENSE_BYTES)
            before = {
                path.name: path.read_bytes()
                for path in repository.iterdir()
            }
            original = FINALIZE.inspect_license_state
            calls = 0

            def fail_final_state(repository: Path, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise FINALIZE.LicenseStateError(
                        "injected post-transition validation failure"
                    )
                return original(repository, **kwargs)

            with mock.patch.object(
                FINALIZE,
                "inspect_license_state",
                fail_final_state,
            ):
                with self.assertRaisesRegex(
                    FINALIZE.LicenseFinalizationError,
                    "rolled back",
                ):
                    FINALIZE.finalize_repository(
                        repository,
                        choice="MIT",
                        license_file=license_file,
                        expected_source_commit=None,
                        apply=True,
                        require_clean_git=False,
                        license_specs=_specs(),
                    )
            self.assertEqual(
                before,
                {path.name: path.read_bytes() for path in repository.iterdir()},
            )
            self.assertEqual(
                VERIFY.inspect_license_state(
                    repository,
                    license_specs=_specs(),
                )["mode"],
                "pending",
            )

    def test_invalid_license_input_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            license_file = root / "candidate.txt"
            license_file.write_bytes(b"wrong\n")
            before = {
                path.name: path.read_bytes()
                for path in repository.iterdir()
            }
            with self.assertRaisesRegex(
                FINALIZE.LicenseFinalizationError,
                "does not match",
            ):
                FINALIZE.finalize_repository(
                    repository,
                    choice="MIT",
                    license_file=license_file,
                    expected_source_commit=None,
                    apply=True,
                    require_clean_git=False,
                    license_specs=_specs(),
                )
            self.assertEqual(
                before,
                {path.name: path.read_bytes() for path in repository.iterdir()},
            )

    def test_write_failure_rolls_back_to_exact_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            license_file = root / "candidate.txt"
            license_file.write_bytes(LICENSE_BYTES)
            before = {
                path.name: path.read_bytes()
                for path in repository.iterdir()
            }
            original = FINALIZE._atomic_replace
            calls = 0

            def fail_first(path: Path, raw: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("injected write failure")
                original(path, raw)

            with mock.patch.object(FINALIZE, "_atomic_replace", fail_first):
                with self.assertRaisesRegex(
                    FINALIZE.LicenseFinalizationError,
                    "rolled back",
                ):
                    FINALIZE.finalize_repository(
                        repository,
                        choice="MIT",
                        license_file=license_file,
                        expected_source_commit=None,
                        apply=True,
                        require_clean_git=False,
                        license_specs=_specs(),
                    )
            self.assertEqual(
                before,
                {path.name: path.read_bytes() for path in repository.iterdir()},
            )
            self.assertEqual(
                VERIFY.inspect_license_state(
                    repository,
                    license_specs=_specs(),
                )["mode"],
                "pending",
            )

    def test_cli_failure_is_bounded_without_traceback(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = FINALIZE.main(
                [
                    "--license",
                    "MIT",
                    "--license-file",
                    str(REPOSITORY_ROOT / "missing-license"),
                    "--expected-source-commit",
                    "0" * 40,
                    "--repository",
                    str(REPOSITORY_ROOT),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("license finalization failed:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
