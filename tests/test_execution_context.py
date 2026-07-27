from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.engine as engine
from benchhandoff.engine import inspect_resume, resume_run, start_run, verify_run
from benchhandoff.errors import BoundaryError, ConfigurationError, EvidenceError
from benchhandoff.model import MAX_CONTEXT_SIZE, load_suite
from benchhandoff.processes import ProcessScopeLaunchError
from benchhandoff.storage import canonical_json_bytes, file_identity, read_json_file
from tests.workspace_temp import WorkspaceTemporaryDirectory

_DESCRIPTOR_BYTES = b"synthetic execution context v2\n"
_DESCRIPTOR = {
    "path": "context.json",
    "media_type": "application/vnd.benchhandoff.test-context+json",
    "digest": f"sha256:{hashlib.sha256(_DESCRIPTOR_BYTES).hexdigest()}",
    "size": len(_DESCRIPTOR_BYTES),
}


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _copy_command_runner(suite_root: Path) -> tuple[Path, str]:
    executable_directory = suite_root / "bin"
    executable_directory.mkdir()
    if os.name == "nt":
        source_text = os.environ.get("COMSPEC")
        if not source_text:
            raise RuntimeError("the command interpreter is unavailable")
        source = Path(source_text)
        destination = executable_directory / "cmd.exe"
    else:
        source_text = shutil.which("sh")
        if not source_text:
            raise RuntimeError("the POSIX shell is unavailable")
        source = Path(source_text)
        destination = executable_directory / "runner"
    shutil.copy2(source, destination)
    if os.name != "nt":
        destination.chmod(
            destination.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
    return destination, destination.name


def _runner_arguments(
    *,
    return_code: int,
    mutate_context: bool = False,
) -> tuple[str, ...]:
    if os.name == "nt":
        command = "ping -n 2 127.0.0.1 >nul & echo evidence>result.txt"
        if mutate_context:
            command += " & echo drift>>context.json"
        if return_code:
            command += f" & exit /b {return_code}"
        return "/d", "/q", "/c", command
    command = "sleep 0.2; printf 'evidence\\n' > result.txt"
    if mutate_context:
        command += "; printf 'drift\\n' >> context.json"
    if return_code:
        command += f"; exit {return_code}"
    return "-c", command


def _write_v2_suite(
    suite_root: Path,
    *,
    return_code: int = 0,
    mutate_context: bool = False,
) -> tuple[Path, Path]:
    suite_root.mkdir()
    (suite_root / _DESCRIPTOR["path"]).write_bytes(_DESCRIPTOR_BYTES)
    executable, executable_argument = _copy_command_runner(suite_root)
    argv = (
        executable_argument,
        *_runner_arguments(
            return_code=return_code,
            mutate_context=mutate_context,
        ),
    )
    suite = suite_root / "suite.toml"
    suite.write_text(
        "\n".join(
            [
                "version = 2",
                'name = "  execution-context-v2  "',
                "",
                "[context]",
                f"path = {_toml_string(_DESCRIPTOR['path'])}",
                f"media_type = {_toml_string(_DESCRIPTOR['media_type'])}",
                f"digest = {_toml_string(_DESCRIPTOR['digest'])}",
                f"size = {_DESCRIPTOR['size']}",
                "",
                "[[task]]",
                'id = "one"',
                "argv = ["
                + ", ".join(_toml_string(value) for value in argv)
                + "]",
                f"inputs = [{_toml_string(_DESCRIPTOR['path'])}]",
                'outputs = ["result.txt"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return suite, executable


def _minimal_suite_text(
    *,
    version: int,
    context_lines: tuple[str, ...] = (),
    extra_root: tuple[str, ...] = (),
) -> str:
    return "\n".join(
        [
            f"version = {version}",
            'name = "strict-context"',
            *extra_root,
            "",
            *context_lines,
            "[[task]]",
            'id = "one"',
            'argv = ["python", "-c", "pass"]',
            f"inputs = [{_toml_string(_DESCRIPTOR['path'])}]",
            'outputs = ["result.txt"]',
            "",
        ]
    )


def _valid_context_lines() -> tuple[str, ...]:
    return (
        "[context]",
        f"path = {_toml_string(_DESCRIPTOR['path'])}",
        f"media_type = {_toml_string(_DESCRIPTOR['media_type'])}",
        f"digest = {_toml_string(_DESCRIPTOR['digest'])}",
        f"size = {_DESCRIPTOR['size']}",
        "",
    )


def _event_types(run_root: Path) -> list[str]:
    return [
        json.loads(line)["type"]
        for line in (run_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]


class _MutateAfterWait:
    def __init__(self, process: object, executable: Path) -> None:
        self._process = process
        self._executable = executable
        self._mutated = False

    @property
    def pid(self) -> int:
        return self._process.pid  # type: ignore[attr-defined, no-any-return]

    def wait(self, *args: object, **kwargs: object) -> int:
        return_code = self._process.wait(*args, **kwargs)  # type: ignore[attr-defined, no-any-return]
        if not self._mutated:
            with self._executable.open("ab") as handle:
                handle.write(b"\x00")
            self._mutated = True
        return return_code

    def __getattr__(self, name: str) -> object:
        return getattr(self._process, name)


class ExecutionContextTests(unittest.TestCase):
    def test_v2_descriptor_is_strict_and_normalized(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="execution-context-parse-") as temporary:
            root = Path(temporary)
            suite, _ = _write_v2_suite(root / "valid")

            parsed = load_suite(suite)

            self.assertEqual(parsed.version, 2)
            self.assertEqual(parsed.name, "execution-context-v2")
            self.assertIsNotNone(parsed.context)
            self.assertEqual(parsed.context.as_dict(), _DESCRIPTOR)
            self.assertEqual(
                parsed.normalized(),
                {
                    "version": 2,
                    "name": "execution-context-v2",
                    "tasks": [parsed.tasks[0].as_dict()],
                    "context": _DESCRIPTOR,
                },
            )

            invalid_cases = {
                "missing-context": _minimal_suite_text(version=2),
                "extra-context-key": _minimal_suite_text(
                    version=2,
                    context_lines=(
                        *_valid_context_lines()[:-1],
                        'unexpected = "no"',
                        "",
                    ),
                ),
                "uppercase-digest": _minimal_suite_text(
                    version=2,
                    context_lines=(
                        "[context]",
                        f"path = {_toml_string(_DESCRIPTOR['path'])}",
                        f"media_type = {_toml_string(_DESCRIPTOR['media_type'])}",
                        f"digest = {_toml_string('sha256:' + ('A' * 64))}",
                        f"size = {_DESCRIPTOR['size']}",
                        "",
                    ),
                ),
                "boolean-size": _minimal_suite_text(
                    version=2,
                    context_lines=(
                        "[context]",
                        f"path = {_toml_string(_DESCRIPTOR['path'])}",
                        f"media_type = {_toml_string(_DESCRIPTOR['media_type'])}",
                        f"digest = {_toml_string(_DESCRIPTOR['digest'])}",
                        "size = true",
                        "",
                    ),
                ),
                "oversized": _minimal_suite_text(
                    version=2,
                    context_lines=(
                        "[context]",
                        f"path = {_toml_string(_DESCRIPTOR['path'])}",
                        f"media_type = {_toml_string(_DESCRIPTOR['media_type'])}",
                        f"digest = {_toml_string(_DESCRIPTOR['digest'])}",
                        f"size = {MAX_CONTEXT_SIZE + 1}",
                        "",
                    ),
                ),
                "empty-media-type": _minimal_suite_text(
                    version=2,
                    context_lines=(
                        "[context]",
                        f"path = {_toml_string(_DESCRIPTOR['path'])}",
                        'media_type = ""',
                        f"digest = {_toml_string(_DESCRIPTOR['digest'])}",
                        f"size = {_DESCRIPTOR['size']}",
                        "",
                    ),
                ),
                "unknown-root-key": _minimal_suite_text(
                    version=2,
                    context_lines=_valid_context_lines(),
                    extra_root=('unknown = "no"',),
                ),
                "v1-context": _minimal_suite_text(
                    version=1,
                    context_lines=_valid_context_lines(),
                ),
                "nonportable-path": _minimal_suite_text(
                    version=2,
                    context_lines=(
                        "[context]",
                        'path = "../outside.json"',
                        f"media_type = {_toml_string(_DESCRIPTOR['media_type'])}",
                        f"digest = {_toml_string(_DESCRIPTOR['digest'])}",
                        f"size = {_DESCRIPTOR['size']}",
                        "",
                    ),
                ),
                "undeclared-path": _minimal_suite_text(
                    version=2,
                    context_lines=(
                        "[context]",
                        'path = "other.json"',
                        f"media_type = {_toml_string(_DESCRIPTOR['media_type'])}",
                        f"digest = {_toml_string(_DESCRIPTOR['digest'])}",
                        f"size = {_DESCRIPTOR['size']}",
                        "",
                    ),
                ),
                "absolute-executable": _minimal_suite_text(
                    version=2,
                    context_lines=_valid_context_lines(),
                ).replace(
                    'argv = ["python", "-c", "pass"]',
                    "argv = ["
                    + ", ".join(
                        _toml_string(value)
                        for value in (sys.executable, "-c", "pass")
                    )
                    + "]",
                ),
            }
            (root / _DESCRIPTOR["path"]).write_bytes(_DESCRIPTOR_BYTES)
            for name, text in invalid_cases.items():
                with self.subTest(name=name):
                    candidate = root / f"{name}.toml"
                    candidate.write_text(text, encoding="utf-8")
                    with self.assertRaises(ConfigurationError):
                        load_suite(candidate)

    def test_v2_descriptor_identity_mismatch_blocks_before_run_creation(self) -> None:
        with WorkspaceTemporaryDirectory(
            prefix="execution-context-descriptor-drift-"
        ) as temporary:
            root = Path(temporary)
            suite, executable = _write_v2_suite(root / "suite")
            descriptor = suite.parent / _DESCRIPTOR["path"]
            descriptor.write_bytes(_DESCRIPTOR_BYTES + b"drift")
            run = root / "run"

            with (
                mock.patch.object(
                    engine.shutil,
                    "which",
                    return_value=str(executable),
                ),
                self.assertRaisesRegex(
                    EvidenceError,
                    "execution-context descriptor identity drifted",
                ),
            ):
                start_run(suite, run)

            self.assertFalse(run.exists())

    def test_unsupported_execution_platform_blocks_before_run_creation(self) -> None:
        with WorkspaceTemporaryDirectory(
            prefix="execution-context-platform-gate-"
        ) as temporary:
            root = Path(temporary)
            suite, _ = _write_v2_suite(root / "suite")
            run = root / "run"

            with (
                mock.patch.object(
                    engine,
                    "require_process_identity_support",
                    side_effect=EvidenceError("synthetic unsupported platform"),
                ),
                mock.patch.object(engine.ProcessScope, "start") as launch,
                self.assertRaisesRegex(
                    EvidenceError,
                    "synthetic unsupported platform",
                ),
            ):
                start_run(suite, run)

            launch.assert_not_called()
            self.assertFalse(run.exists())

    def test_unencodable_resolved_executable_is_a_boundary_error(self) -> None:
        with WorkspaceTemporaryDirectory(
            prefix="execution-context-path-encoding-"
        ) as temporary:
            root = Path(temporary)
            suite_path, _ = _write_v2_suite(root / "suite")
            suite = load_suite(suite_path)
            unencodable = Path("unencodable-\udcff")

            with (
                mock.patch.object(
                    engine.shutil,
                    "which",
                    return_value="synthetic-runner",
                ),
                mock.patch.object(
                    Path,
                    "resolve",
                    return_value=unencodable,
                ),
                mock.patch.object(
                    engine,
                    "file_identity",
                    return_value={"sha256": "0" * 64, "size": 1},
                ),
                self.assertRaisesRegex(
                    BoundaryError,
                    "executable path is not valid UTF-8 text",
                ),
            ):
                engine._resolved_executable(suite, suite.tasks[0])

    def test_v2_binds_real_executable_identity_without_a_path(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="execution-context-bind-") as temporary:
            root = Path(temporary)
            suite, executable = _write_v2_suite(root / "suite")
            run = root / "run"

            with mock.patch.object(
                engine.shutil,
                "which",
                return_value=str(executable),
            ):
                result = start_run(suite, run)
                verified = verify_run(run)

            self.assertEqual(result.status, "completed")
            self.assertEqual(verified["status"], "verified")
            plan = read_json_file(run / "plan.json", label="plan")
            task_context = plan["execution_context"]["tasks"]["one"]
            executable_record = task_context["executable"]
            normalized_executable_path = os.path.normcase(
                str(executable.resolve(strict=True))
            ).encode("utf-8")
            self.assertEqual(
                executable_record,
                {
                    "basename": executable.name,
                    "path_sha256": hashlib.sha256(
                        normalized_executable_path
                    ).hexdigest(),
                    "path_utf8_size": len(normalized_executable_path),
                    **file_identity(executable, label="bound executable"),
                },
            )
            self.assertEqual(
                set(executable_record),
                {
                    "basename",
                    "path_sha256",
                    "path_utf8_size",
                    "sha256",
                    "size",
                },
            )
            self.assertEqual(
                Path(executable_record["basename"]).name,
                executable_record["basename"],
            )
            self.assertFalse(Path(executable_record["basename"]).is_absolute())
            self.assertNotIn("path", executable_record)
            self.assertEqual(task_context["descriptor"], _DESCRIPTOR)
            environment = task_context["environment"]
            self.assertIs(environment["inherit_parent"], False)
            self.assertEqual(
                environment["runner_variables"],
                [
                    "BENCHHANDOFF_ATTEMPT",
                    "BENCHHANDOFF_RUN_ID",
                    "BENCHHANDOFF_TASK_ID",
                ],
            )
            expected_static_names = {"SystemRoot"} if os.name == "nt" else set()
            self.assertEqual(
                set(environment["static_variables"]),
                expected_static_names,
            )
            for identity in environment["static_variables"].values():
                self.assertEqual(set(identity), {"sha256", "utf8_size"})
            process_scope = task_context["process_scope"]
            if os.name == "nt":
                self.assertEqual(
                    process_scope,
                    {"mode": "windows-job", "cooperative": False},
                )
            else:
                self.assertEqual(
                    process_scope,
                    {
                        "mode": "posix-cooperative-process-group",
                        "cooperative": True,
                    },
                )
            expected_task_digest = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "descriptor": _DESCRIPTOR,
                        "environment": environment,
                        "executable": executable_record,
                        "process_scope": process_scope,
                    }
                )
            ).hexdigest()
            self.assertEqual(task_context["context_sha256"], expected_task_digest)
            expected_suite_digest = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "descriptor": _DESCRIPTOR,
                        "tasks": plan["execution_context"]["tasks"],
                    }
                )
            ).hexdigest()
            self.assertEqual(
                plan["execution_context"]["context_sha256"],
                expected_suite_digest,
            )
            state = read_json_file(run / "state.json", label="state")
            attempt = state["tasks"]["one"]["attempts"][0]
            self.assertEqual(
                attempt["execution_context_sha256"],
                expected_task_digest,
            )
            self.assertEqual(
                attempt["process_scope"],
                {
                    **process_scope,
                    "scope_id": attempt["child_pid"],
                    "empty_confirmed": True,
                    "closure": "natural-empty",
                },
            )
            bundle = read_json_file(run / "bundle.json", label="bundle")
            self.assertEqual(bundle["execution_context"], plan["execution_context"])

    def test_prelaunch_executable_drift_fails_before_task_state_advances(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="execution-context-prelaunch-") as temporary:
            root = Path(temporary)
            suite, executable = _write_v2_suite(root / "suite")
            run = root / "run"
            real_prepare = engine.prepare_new_directory

            def prepare_then_drift(path: Path | str, *, label: str) -> Path:
                prepared = real_prepare(path, label=label)
                with executable.open("ab") as handle:
                    handle.write(b"\x00")
                return prepared

            with (
                mock.patch.object(
                    engine.shutil,
                    "which",
                    return_value=str(executable),
                ),
                mock.patch.object(
                    engine,
                    "prepare_new_directory",
                    side_effect=prepare_then_drift,
                ),
            ):
                with self.assertRaisesRegex(
                    EvidenceError,
                    "execution context drifted before launch",
                ):
                    start_run(suite, run)

            state = read_json_file(run / "state.json", label="state")
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["tasks"]["one"]["status"], "pending")
            self.assertEqual(state["tasks"]["one"]["attempts"], [])
            self.assertEqual(_event_types(run), ["run_started"])
            self.assertFalse((suite.parent / "result.txt").exists())
            self.assertFalse((run / "bundle.json").exists())

    def test_confirmed_scope_launch_cleanup_is_a_terminal_failed_attempt(
        self,
    ) -> None:
        with WorkspaceTemporaryDirectory(
            prefix="execution-context-launch-cleaned-"
        ) as temporary:
            root = Path(temporary)
            suite, executable = _write_v2_suite(root / "suite")
            run = root / "run"
            with (
                mock.patch.object(
                    engine.shutil,
                    "which",
                    return_value=str(executable),
                ),
                mock.patch.object(
                    engine.ProcessScope,
                    "start",
                    side_effect=ProcessScopeLaunchError(
                        "synthetic confirmed cleanup"
                    ),
                ),
            ):
                result = start_run(suite, run)

            self.assertEqual(result.status, "failed")
            state = read_json_file(run / "state.json", label="state")
            attempt = state["tasks"]["one"]["attempts"][0]
            self.assertEqual(attempt["status"], "failed")
            self.assertIsNone(attempt["child_pid"])
            self.assertIsNone(attempt["child_start_token"])
            self.assertFalse(attempt["child_launch_guard"])
            self.assertEqual(
                attempt["process_scope"]["closure"],
                "launch-cleaned",
            )
            self.assertTrue(attempt["process_scope"]["empty_confirmed"])
            self.assertFalse((run / "bundle.json").exists())

    def test_post_exit_executable_drift_fails_before_output_hash_or_completion(
        self,
    ) -> None:
        with WorkspaceTemporaryDirectory(prefix="execution-context-postrun-") as temporary:
            root = Path(temporary)
            suite, executable = _write_v2_suite(root / "suite")
            run = root / "run"
            real_popen = engine.subprocess.Popen
            real_member_identity = engine.member_identity

            def popen_then_mutate(*args: object, **kwargs: object) -> _MutateAfterWait:
                return _MutateAfterWait(real_popen(*args, **kwargs), executable)

            def reject_output_hash(
                root_path: Path | str,
                relative: str,
                *,
                label: str,
            ) -> dict[str, object]:
                if label.startswith("output "):
                    self.fail("output hashing occurred after execution-context drift")
                return real_member_identity(root_path, relative, label=label)

            with (
                mock.patch.object(
                    engine.subprocess,
                    "Popen",
                    side_effect=popen_then_mutate,
                ),
                mock.patch.object(
                    engine,
                    "member_identity",
                    side_effect=reject_output_hash,
                ),
                mock.patch.object(
                    engine.shutil,
                    "which",
                    return_value=str(executable),
                ),
            ):
                result = start_run(suite, run)

            self.assertEqual(result.status, "failed")
            state = read_json_file(run / "state.json", label="state")
            task = state["tasks"]["one"]
            self.assertEqual(task["status"], "failed")
            self.assertEqual(len(task["attempts"]), 1)
            attempt = task["attempts"][0]
            self.assertEqual(attempt["status"], "failed")
            self.assertNotIn("verified_outputs", attempt)
            self.assertIn(
                "post-exit execution-context validation failed",
                attempt["error"],
            )
            self.assertIn("execution context drifted", attempt["error"])
            self.assertEqual(
                _event_types(run),
                ["run_started", "task_started", "task_failed"],
            )
            self.assertTrue((suite.parent / "result.txt").is_file())
            self.assertFalse((run / "bundle.json").exists())

    def test_nonzero_child_context_drift_is_recorded_before_terminal_classification(
        self,
    ) -> None:
        with WorkspaceTemporaryDirectory(
            prefix="execution-context-nonzero-drift-"
        ) as temporary:
            root = Path(temporary)
            suite, executable = _write_v2_suite(
                root / "suite",
                return_code=75,
                mutate_context=True,
            )
            run = root / "run"

            with mock.patch.object(
                engine.shutil,
                "which",
                return_value=str(executable),
            ):
                result = start_run(suite, run)

            self.assertEqual(result.status, "failed")
            state = read_json_file(run / "state.json", label="state")
            attempt = state["tasks"]["one"]["attempts"][0]
            self.assertEqual(attempt["status"], "failed")
            self.assertEqual(attempt["return_code"], 75)
            self.assertTrue(attempt["process_scope"]["empty_confirmed"])
            self.assertNotIn("verified_outputs", attempt)
            self.assertIn(
                "post-exit execution-context validation failed",
                attempt["error"],
            )
            self.assertIn(
                "execution-context descriptor identity drifted",
                attempt["error"],
            )
            self.assertIn(
                "child outcome: child process exited non-zero (75)",
                attempt["error"],
            )
            self.assertFalse((run / "bundle.json").exists())

    def test_resume_context_drift_rejects_before_resumed_transition(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="execution-context-resume-") as temporary:
            root = Path(temporary)
            suite, executable = _write_v2_suite(root / "suite", return_code=75)
            run = root / "run"
            resolver = mock.patch.object(
                engine.shutil,
                "which",
                return_value=str(executable),
            )
            with resolver:
                self.assertEqual(start_run(suite, run).status, "failed")
                decision = inspect_resume(run)
            plan = read_json_file(run / "plan.json", label="plan")
            self.assertEqual(
                decision["next_task"]["execution_context"],
                plan["execution_context"]["tasks"]["one"],
            )
            state_before = (run / "state.json").read_bytes()
            events_before = (run / "events.jsonl").read_bytes()
            attempts_before = len(
                read_json_file(run / "state.json", label="state")["tasks"]["one"][
                    "attempts"
                ]
            )
            descriptor = suite.parent / _DESCRIPTOR["path"]
            descriptor.write_bytes(_DESCRIPTOR_BYTES + b"drift")

            with (
                mock.patch.object(
                    engine.shutil,
                    "which",
                    return_value=str(executable),
                ),
                self.assertRaisesRegex(
                    EvidenceError,
                    "identity drifted",
                ),
            ):
                resume_run(
                    run,
                    expected_decision_sha256=decision["decision_sha256"],
                )

            self.assertEqual((run / "state.json").read_bytes(), state_before)
            self.assertEqual((run / "events.jsonl").read_bytes(), events_before)
            self.assertNotIn("run_resumed", _event_types(run))
            state = read_json_file(run / "state.json", label="state")
            self.assertEqual(
                len(state["tasks"]["one"]["attempts"]),
                attempts_before,
            )
            self.assertEqual(list((run / "quarantine").iterdir()), [])

    def test_existing_v1_example_still_runs_and_verifies(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="execution-context-v1-") as temporary:
            root = Path(temporary)
            suite_root = root / "suite"
            shutil.copytree(REPOSITORY_ROOT / "examples" / "basic", suite_root)
            run = root / "run"

            result = start_run(suite_root / "suite.toml", run)

            self.assertEqual(result.status, "completed")
            self.assertEqual(verify_run(run)["status"], "verified")
            plan = read_json_file(run / "plan.json", label="plan")
            bundle = read_json_file(run / "bundle.json", label="bundle")
            self.assertEqual(plan["schema_version"], 1)
            self.assertEqual(plan["suite"]["version"], 1)
            self.assertNotIn("context", plan["suite"])
            self.assertNotIn("execution_context", plan)
            self.assertNotIn("execution_context", bundle)
            self.assertTrue((suite_root / "output.json").is_file())


if __name__ == "__main__":
    unittest.main()
